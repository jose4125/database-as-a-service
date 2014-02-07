# -*- coding: utf-8 -*-
from __future__ import absolute_import

from django.conf import settings

import os
import logging
from system.models import Configuration
from celery import states
from celery.utils.log import get_task_logger
from celery.exceptions import SoftTimeLimitExceeded
from dbaas.celery import app

from util import call_script
from util import notifications
from .util import get_clone_args
from .models import TaskHistory
from drivers import factory_for

LOG = get_task_logger(__name__)

def get_history_for_task_id(task_id):
    try:
        return TaskHistory.objects.get(task_id=task_id)
    except Exception, e:
        LOG.error("could not find history for task id %s" % task_id)
        return None

@app.task(bind=True)
def clone_database(self, origin_database, dest_database, user=None):
    
    #register History
    task_history = TaskHistory.register(request=self.request, user=user)
    
    LOG.info("origin_database: %s" % origin_database)
    LOG.info("dest_database: %s" % dest_database)
    # task_state = self.AsyncResult(self.request.id).state)
    LOG.info("id: %s | task: %s | kwargs: %s | args: %s" % (self.request.id,
                                                            self.request.task,
                                                            self.request.kwargs,
                                                            str(self.request.args)))

    args = get_clone_args(origin_database, dest_database)

    try:
        script_name = factory_for(origin_database.databaseinfra).clone()
        #script_name = "dummy_clone.sh"
        return_code, output = call_script(script_name, working_dir=settings.SCRIPTS_PATH, args=args)
        LOG.info("%s - return code: %s" % (self.request.id, return_code))
        if return_code != 0:
            task_history.update_status_for(TaskHistory.STATUS_ERROR, details=output)
            LOG.error("task id %s - error occurred. Transaction rollback" % self.request.id)
            dest_database.is_in_quarantine = True
            dest_database.save()
            dest_database.delete()
        else:
            task_history.update_status_for(TaskHistory.STATUS_SUCCESS)
    except SoftTimeLimitExceeded:
        LOG.error("task id %s - timeout exceeded" % self.request.id)
        task_history.update_status_for(TaskHistory.STATUS_ERROR)
    except Exception, e:
        LOG.error("task id %s error: %s" % (self.request.id, e))
        task_history.update_status_for(TaskHistory.STATUS_ERROR)

    return
    

@app.task
def databaseinfra_notification():
    from physical.models import DatabaseInfra
    from django.db.models import Sum, Count
    from django.core.cache import cache
    # lock the task when someone worker is runing
    lock_id = "lock_databaseinfra_notification"
    acquire_lock = lambda: cache.add(lock_id, "true", 60) # seconds
    release_lock = lambda: cache.delete(lock_id)
    if acquire_lock():
        try:
            infras = DatabaseInfra.objects.values('plan__name', 'environment__name', 'engine__engine_type__name').annotate(capacity=Sum('capacity'))
            for infra in infras:
                used = DatabaseInfra.objects.filter(plan__name=infra['plan__name'], environment__name=infra['environment__name'], engine__engine_type__name=infra['engine__engine_type__name']).aggregate(used=Count('databases'))
                percent = int(used['used'] * 100 / infra['capacity'])
                if percent >= Configuration.get_by_name_as_int("threshold_infra_notification", default=50):
                    LOG.info('Plan %s in environment %s with %s%% occupied' % (infra['plan__name'], infra['environment__name'],percent))
                    LOG.info("Sending notification...")
                    notifications.databaseinfra_ending(infra['plan__name'], infra['environment__name'], used['used'],infra['capacity'],percent)
        finally:
            release_lock()
        return

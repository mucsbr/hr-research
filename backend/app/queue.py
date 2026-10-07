from celery import Celery

from app.config import get_settings

celery = Celery(
    "hr_intelligence",
    broker=get_settings().redis_url.get_secret_value(),
    include=["app.workers"],
)
celery.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_ignore_result=True,
    timezone="Asia/Shanghai",
    enable_utc=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_connection_retry_on_startup=True,
    broker_transport_options={"visibility_timeout": 1200},
    beat_schedule={
        "dispatch-pending": {"task": "app.dispatch_pending", "schedule": 10.0},
        "run-schedules": {"task": "app.run_schedules", "schedule": 30.0},
        "deliver-notifications": {"task": "app.deliver_notifications", "schedule": 15.0,
                                  "options": {"queue": "notifications"}},
        "dispatch-maintenance": {"task": "app.dispatch_maintenance", "schedule": 60.0,
                                 "options": {"queue": "maintenance"}},
        "retain-data": {"task": "app.run_retention", "schedule": 3600.0,
                        "options": {"queue": "maintenance"}},
    },
)

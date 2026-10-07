"""Database fencing for delayed results from an older task execution."""
from sqlalchemy import select

from app.collection import CollectionError
from app.models import TaskRun


def active_execution(db, task_id, expected_attempt, *, allow_cancel=False):
    task = db.scalar(select(TaskRun).where(TaskRun.id == task_id).with_for_update()
                     .execution_options(populate_existing=True))
    if not task or task.attempt != expected_attempt or task.status != "running":
        raise CollectionError("stale_execution", "该执行已被接管或结束，旧结果不会写入")
    if task.cancel_requested and not allow_cancel:
        raise CollectionError("cancelled", "任务已取消，未提交新的采集结果")
    return task

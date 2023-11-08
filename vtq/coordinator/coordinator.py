import logging
import time
import peewee
from vtq import task_queue
from vtq import model
from vtq import channel
from vtq import configuration
from vtq.task import Task
from vtq.coordinator import task as task_mod

logger = logging.getLogger(name=__name__)


_INVISIBLE_TIMESTAMP_SECONDS = 2**31 - 1


class Coordinator(task_queue.TaskQueue):
    def __init__(self, workspace: str = "default"):
        self._db = model.get_sqlite_database()
        cls_factory = model.ModelClsFactory(workspace=workspace, database=self._db)
        self._vq_cls = cls_factory.generate_virtual_queue_cls()
        self._task_cls = cls_factory.generate_task_cls(self._vq_cls)
        self._task_error_cls = cls_factory.generate_task_error_cls(self._task_cls)

        self._channel = channel.Channel()
        self._config_fetcher = configuration.ConfigurationFetcher(workspace=workspace)

    def enqueue(
        self,
        task_data: bytes,
        vqueue_name: str = "",
        priority: int = 50,
        delay_millis: int = 0,
    ) -> str:
        """Insert the task data into the SQL table. Then publish the event that task is added."""
        visible_at = time.time() + delay_millis / 1000.0 if delay_millis else 0

        with self._db:
            try:
                task: model.Task = self._task_cls.create(
                    data=task_data,
                    vqueue_name=vqueue_name,
                    priority=priority,
                    visible_at=visible_at,
                )
            except peewee.IntegrityError as e:
                if e.args[0] != "FOREIGN KEY constraint failed":
                    raise
                logger.warning(f"VQ {vqueue_name} doesn't exists")

                task = self._enqueue_task_with_new_vq(
                    task_data, vqueue_name, priority, visible_at
                )
        self._channel.send(task.id)
        return task.id

    def _enqueue_task_with_new_vq(
        self, task_data, vqueue_name, priority, visible_at
    ) -> model.Task:
        with self._db.atomic():
            vq_config = self._config_fetcher.configuration_for(vqueue_name)
            self._vq_cls.insert(
                name=vqueue_name,
                priority=vq_config.priority,
                bucket_name=vq_config.bucket.name,
                bucket_weight=vq_config.bucket.weight,
                visibility_timeout=vq_config.visibility_timeout_seconds,
            ).execute()
            task: model.Task = self._task_cls.create(
                data=task_data,
                vqueue_name=vqueue_name,
                priority=priority,
                visible_at=visible_at,
            )
        return task

    def receive(self, max_number: int = 1, wait_time_seconds: int = 0) -> list[Task]:
        """Get tasks from the SQL table, then update the VQ `hidden` status by the result from the Rate Limit."""
        tasks: list[Task] = []
        while len(tasks) < max_number:
            task = self._read()
            if not task:
                if tasks or wait_time_seconds <= 0:
                    return tasks
                # block to wait at most `wait_time_seconds`
                raise NotImplementedError
            tasks.append(task)

        return tasks

    def _read(self) -> Task | None:
        """Get a task from the SQL table, then update the VQ `hidden` status by the result from the Rate Limit."""
        current_ts = time.time()
        fn = peewee.fn
        task_cls = self._task_cls
        vq_cls = self._vq_cls
        with self._db:
            # select the max priority layer from the avaible VQs and available tasks.
            available_task_query = (
                task_cls.select(task_cls, vq_cls)
                .join(vq_cls)
                .where((~vq_cls.hidden) & (current_ts >= task_cls.visible_at))
            )

            max_vq_priority = available_task_query.select(
                fn.max(vq_cls.priority).alias("max_vq_priority")
            )

            priority_layer_query = available_task_query.where(
                vq_cls.priority == max_vq_priority
            )

            # TODO: implement bucket random weighted-priority selection
            # d = (
            #     priority_layer_query.select(vq_cls.bucket_name)
            #     .group_by(vq_cls.bucket_name)
            #     .dicts()
            # )
            # print(list(d))

            task: model.Task | None = (
                priority_layer_query.select(
                    task_cls.id,
                    task_cls.data,
                    task_cls.vqueue_name,
                    task_cls.priority,  # for debug
                    vq_cls.visibility_timeout.alias("vq_visibility_timeout"),
                )
                .order_by(
                    self._task_cls.priority.desc(), self._task_cls.queued_at.asc()
                )
                .objects()  # there is a peewee bug, the Task model only has id/priority property populated, but without vqueue_name.
                .first()
            )
            print(task)
            if not task:
                return
            print(task.id, task.vqueue_name, task.priority)

        # TODO: check rate limit

        # Update task status and virtual queue status
        # TODO: check task.vqueue_name & task.update_at & vq.update_at
        with self._db:
            with self._db.atomic():
                vq_query = vq_cls.select(vq_cls.name).where(
                    ~vq_cls.hidden & (vq_cls.name == task.vqueue_name)
                )
                task_cls.update(
                    status=50, visible_at=task.vq_visibility_timeout + current_ts
                ).where(
                    (task_cls.vqueue_name == vq_query)
                    & (task_cls.id == task.id)
                    & (task_cls.status < 10)
                    & (task_cls.visible_at <= current_ts)
                ).execute()
                # TODO: update VQ hidden according to Rate limit policy
        return Task(task.id, task.data)

    def _get_task_only_status(self, task_id) -> model.Task | None:
        task: model.Task | None = (
            self._task_cls.select(self._task_cls.id, self._task_cls.status)
            .where(self._task_cls.id == task_id)
            .first()
        )
        return task

    def ack(self, task_id: str) -> bool:
        with self._db:
            task = self._get_task_only_status(task_id)
            if not task:
                return False
            if task_mod.is_succeeded(task):
                return True
            if not task_mod.is_wip(task):
                return False

            # TODO: change to conditional atomic update, using where clause with update
            task.status = 100
            task.visible_at = _INVISIBLE_TIMESTAMP_SECONDS
            task.ended_at = time.time()
            task.save()
        return True

    def nack(self, task_id: str, error_messsage: str) -> bool:
        with self._db:
            task = self._get_task_only_status(task_id)
            if not task:
                return False
            if task_mod.is_failed(task):
                return True
            if not task_mod.is_wip(task):
                return False

            # TODO: change to conditional atomic update, using where clause with update
            with self._db.atomic():
                current_ts = time.time()
                task.status = 101
                task.visible_at = _INVISIBLE_TIMESTAMP_SECONDS
                task.ended_at = current_ts
                task.save()
                if error_messsage:
                    self._task_error_cls.create(
                        task_id=task.id,
                        error_messsage=error_messsage,
                        happended_at=current_ts,
                    )
        return True

    def requeue(self, task_id: str) -> bool:
        with self._db:
            task = self._get_task_only_status(task_id)
            if not task:
                return False
            if task.is_unstarted():
                return True
            if not task.is_wip():
                return False

            # TODO

    def retry(
        self, task_id: str, delayMillis: int = 0, error_message: str = ""
    ) -> bool:
        return super().retry(task_id, delayMillis, error_message)

    def __len__(self):
        return super().__len__()

    def delete(self, task_id: str):
        return super().delete(task_id)

    def update(self, task_id: str, **kwargs):
        return super().update(task_id, **kwargs)


if __name__ == "__main__":
    logging.basicConfig()
    model.enable_debug_logging(disable_handler=True)
    c = Coordinator()
    task_id = c.enqueue(task_data=b"123")
    print(task_id)
    # print(c.ack(task_id))
    print(c.receive())
    print(c.receive())

import logging
import signal
from time import sleep

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count, Q
from qfieldcloud.core.models import Job
from worker_wrapper.wrapper import (
    DeltaApplyJobRun,
    PackageJobRun,
    ProcessProjectfileJobRun,
)

SECONDS = 5


class GracefulKiller:
    alive = True

    def __init__(self):
        signal.signal(signal.SIGINT, self._kill)
        signal.signal(signal.SIGTERM, self._kill)

    def _kill(self, *args):
        self.alive = False


class Command(BaseCommand):
    help = "Dequeue QFieldCloud Jobs from the DB"

    def add_arguments(self, parser):
        parser.add_argument(
            "--single-shot", action="store_true", help="Don't run infinite loop."
        )

    def handle(self, *args, **options):
        logging.info("Dequeue QFieldCloud Jobs from the DB")
        killer = GracefulKiller()

        while killer.alive:
            # the worker-wrapper caches outdated ContentType ids during tests since
            # the worker-wrapper and the tests reside in different containers
            if settings.DATABASES["default"]["NAME"].startswith("test_"):
                ContentType.objects.clear_cache()

            queued_job = None

            with transaction.atomic():

                busy_projects_ids_qs = (
                    Job.objects.filter(
                        status=Job.Status.PENDING,
                    )
                    .annotate(
                        active_jobs_count=Count(
                            "project__jobs",
                            filter=Q(
                                project__jobs__status__in=[
                                    Job.Status.QUEUED,
                                    Job.Status.STARTED,
                                ]
                            ),
                        )
                    )
                    .filter(active_jobs_count__gt=0)
                    .values("project_id")
                )

                # select all the pending jobs, that their project has no other active job
                jobs_qs = (
                    Job.objects.select_for_update(skip_locked=True)
                    .filter(status=Job.Status.PENDING)
                    .exclude(project_id__in=busy_projects_ids_qs)
                    .order_by("created_at")
                )

                # each `worker_wrapper` or `dequeue.py` script can handle only one job and we handle the oldest
                queued_job = jobs_qs.first()

                # there might be no jobs in the queue
                if queued_job:
                    logging.info(f"Dequeued job {queued_job.id}, run!")
                    queued_job.status = Job.Status.QUEUED
                    queued_job.save()

            if queued_job:
                self._run(queued_job)
                queued_job = None
            else:
                if options["single_shot"]:
                    break

                for _i in range(SECONDS):
                    if killer.alive:
                        sleep(1)

            if options["single_shot"]:
                break

    def _run(self, job: Job):
        job_run_classes = {
            Job.Type.PACKAGE: PackageJobRun,
            Job.Type.DELTA_APPLY: DeltaApplyJobRun,
            Job.Type.PROCESS_PROJECTFILE: ProcessProjectfileJobRun,
        }

        if job.type in job_run_classes:
            job_run_class = job_run_classes[job.type]
        else:
            raise NotImplementedError(f"Unknown job type {job.type}")

        job_run = job_run_class(job.id)
        job_run.run()

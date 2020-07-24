import datetime
import logging
import sys
import traceback

from django.conf import settings
from django.core.mail import mail_admins
from django.db.models.signals import post_save, pre_delete

from multidb.pinning import pin_this_thread, unpin_this_thread

from fjord.celery import app
from fjord.search.index import index_chunk
from fjord.search.models import Record
from fjord.search.utils import from_class_path, to_class_path


log = logging.getLogger('i.task')


@app.task()
def index_chunk_task(index, batch_id, rec_id, chunk):
    """Index a chunk of things.

    :arg index: the name of the index to index to
    :arg batch_id: the name for the batch this chunk belongs to
    :arg rec_id: the id for the record for this task
    :arg chunk: a (cls_path, id_list) of things to index
    """
    cls_path, id_list = chunk
    cls = from_class_path(cls_path)
    rec = None

    try:
        # Pin to main db to avoid replication lag issues and stale
        # data.
        pin_this_thread()

        # Update record data.
        rec = Record.objects.get(pk=rec_id)
        rec.start_time = datetime.datetime.now()
        rec.message = u'Reindexing into %s' % index
        rec.status = Record.STATUS_IN_PROGRESS
        rec.save()

        index_chunk(cls, id_list)

        rec.mark_success()

    except Exception:
        if rec is not None:
            rec.mark_fail(u'Errored out %s %s' % (
                sys.exc_type, sys.exc_value))
        raise

    finally:
        unpin_this_thread()


# Note: If you reduce the length of RETRY_TIMES, it affects all tasks
# currently in the celery queue---they'll throw an IndexError.
RETRY_TIMES = (
    60,  # 1 minute
    5 * 60,  # 5 minutes
    10 * 60,  # 10 minutes
    30 * 60,  # 30 minutes
    60 * 60,  # 60 minutes
    )
MAX_RETRIES = len(RETRY_TIMES)


@app.task()
def index_item_task(cls_path, item_id, **kwargs):
    """Index an item given it's DocType cls_path and id"""
    doctype = from_class_path(cls_path)
    retries = kwargs.get('task_retries', 0)
    log.debug('Index attempt #%s', retries)
    try:
        resp = doctype.get_model().objects.get(id=item_id)
        doc = doctype.extract_doc(resp)
        doctype.docs.bulk_index(docs=[doc])

    except Exception as exc:
        log.exception('Error while live indexing %s %d: %s',
                      doctype, item_id, exc)
        if retries >= MAX_RETRIES:
            raise
        retry_time = RETRY_TIMES[retries]

        args = (cls_path, item_id)
        if not kwargs:
            # Celery requires that kwargs be non empty, but when EAGER
            # is true, it provides empty kwargs. Yay.
            kwargs['_dummy'] = True
        index_item_task.retry(args, kwargs, exc, countdown=retry_time)


@app.task()
def unindex_item_task(cls_path, item_id, **kwargs):
    """Remove item from index, given it's DocType class_path and id"""
    doctype = from_class_path(cls_path)
    try:
        doctype.docs.delete(item_id)

    except Exception as exc:
        retries = kwargs.get('task_retries', 0)
        log.exception('Error while live unindexing %s %d: %s',
                      doctype, item_id, exc)
        if retries >= MAX_RETRIES:
            raise
        retry_time = RETRY_TIMES[retries]

        args = (doctype, item_id)
        if not kwargs:
            # Celery is lame. It requires that kwargs be non empty, but when
            # EAGER is true, it provides empty kwargs.
            kwargs['_dummy'] = True
        unindex_item_task.retry(args, kwargs, exc, countdown=retry_time)


def _live_index_handler(sender, **kwargs):
    if (not settings.ES_LIVE_INDEX
            or 'signal' not in kwargs
            or 'instance' not in kwargs):
        return

    instance = kwargs['instance']

    try:
        if kwargs['signal'] == post_save:
            cls_path = to_class_path(instance.get_doctype())
            index_item_task.delay(cls_path, instance.id)

        elif kwargs['signal'] == pre_delete:
            cls_path = to_class_path(instance.get_doctype())
            unindex_item_task.delay(cls_path, instance.id)

    except Exception:
        # At this point, we're trying to create an indexing task for
        # some response that's changed. When an indexing task is
        # created, it uses amqp to connect to rabbitmq to put the
        # new task in the queue. If a user is leaving feadback and
        # this fails (which it does with some regularity), the user
        # gets an HTTP 500 which stinks.
        #
        # The problem is exacerbated by the fact I don't know the full
        # list of exceptions that can get kicked up here. So what
        # we're going to do is catch them all, look for "amqp" in the
        # frames and if it's there, we'll ignore the exception and
        # send an email. We can collect reasons and narrow this down
        # at some point if that makes sense to do. If "amqp" is not in
        # the frames, then it's some other kind of error that we want
        # to show up, so we'll re-raise it. Sorry, user!
        #
        # In this way, users will stop seeing HTTP 500 errors during
        # rabbitmq outages.
        exc_type, exc_value, exc_tb = sys.exc_info()
        frames = traceback.extract_tb(exc_tb)
        for fn, ln, fun, text in frames:
            if 'amqp' in fn:
                # This is an amqp frame which indicates that we
                # should ignore this and send an email.
                mail_admins(
                    subject='amqp error',
                    message=(
                        'amqp error:\n\n' +
                        traceback.format_exc()
                    )
                )
                return

        # No amqp frames, so re-raise it.
        raise


def register_live_index(model_cls):
    """Register a model and index for auto indexing."""
    uid = str(model_cls) + 'live_indexing'
    post_save.connect(_live_index_handler, model_cls, dispatch_uid=uid)
    pre_delete.connect(_live_index_handler, model_cls, dispatch_uid=uid)
    # Enable this to be used as decorator.
    return model_cls

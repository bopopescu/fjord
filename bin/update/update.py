"""
Deploys fjord for input.mozilla.org

Requires commander_ which is installed on the systems that need it.

.. _commander: https://github.com/oremj/commander
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from commander.deploy import task, hostgroups
import commander_settings as settings

PYTHON = getattr(settings, 'PYTHON_PATH', 'python2.6')


@task
def update_code(ctx, tag):
    """Update the code to a specific git reference (tag/sha/etc)."""
    with ctx.lcd(settings.SRC_DIR):
        ctx.local('git fetch')
        ctx.local('git checkout -f %s' % tag)
        ctx.local('git submodule sync')
        ctx.local('git submodule update --init --recursive')


@task
def update_product_details(ctx):
    """Update mozilla product details"""
    with ctx.lcd(settings.SRC_DIR):
        ctx.local(PYTHON + ' manage.py update_product_details -f')


@task
def update_locales(ctx):
    """Update a locale directory from Git.

    Assumes localizations:

    1) exist,
    2) are in Git,
    3) are in SRC_DIR/locale, and
    4) have a compile-mo.sh script

    This should all be pretty standard, but change it if you need to.

    """
    # Do an git pull to get the latest .po files.
    with ctx.lcd(os.path.join(settings.SRC_DIR, 'locale')):
        ctx.local('git pull origin main')

    # Run the script that lints the .po files and compiles to .mo the
    # the ones that don't have egregious errors in them. This prints
    # stdout to the deploy log and also to media/postatus.txt so
    # others can see what happened.
    with ctx.lcd(settings.SRC_DIR):
        ctx.local('date > media/postatus.txt')
        ctx.local('bin/compile-linted-mo.sh -p %s | /usr/bin/tee -a media/postatus.txt' % PYTHON)


@task
def update_assets(ctx):
    with ctx.lcd(settings.SRC_DIR):
        # Delete existing static files so that django-pipeline rebuilds
        # them correctly.
        ctx.local('git clean -fxd -- static')
        ctx.local(PYTHON + ' manage.py collectstatic --noinput')


@task
def update_db(ctx):
    """Update the database schema, if necessary."""
    with ctx.lcd(settings.SRC_DIR):
        ctx.local(PYTHON + ' manage.py migrate --noinput')


@task
def update_cron(ctx):
    with ctx.lcd(settings.SRC_DIR):
        if getattr(settings, 'PYTHON_PATH', None) is not None:
            # FIXME: Temporary until all servers have PYTHON_PATH.
            ctx.local(PYTHON + ' ./bin/crontab/gen-crons.py -p %s -w %s -s %s -u apache > /etc/cron.d/.%s' %
                      (settings.PYTHON_PATH, settings.WWW_DIR, settings.SRC_DIR, settings.CRON_NAME))
        else:
            ctx.local(PYTHON + ' ./bin/crontab/gen-crons.py -w %s -s %s -u apache > /etc/cron.d/.%s' %
                      (settings.WWW_DIR, settings.SRC_DIR, settings.CRON_NAME))
        ctx.local('mv /etc/cron.d/.%s /etc/cron.d/%s' % (settings.CRON_NAME, settings.CRON_NAME))


@task
def checkin_changes(ctx):
    """Use the local, IT-written deploy script to check in changes."""
    ctx.local(settings.DEPLOY_SCRIPT)


@hostgroups(settings.WEB_HOSTGROUP, remote_kwargs={'ssh_key': settings.SSH_KEY})
def deploy_app(ctx):
    """Call the remote update script to push changes to webheads."""
    ctx.remote(settings.REMOTE_UPDATE_SCRIPT)
    ctx.remote('/bin/touch %s' % settings.REMOTE_WSGI)


@hostgroups(settings.CELERY_HOSTGROUP, remote_kwargs={'ssh_key': settings.SSH_KEY})
def update_celery(ctx):
    """Update and restart Celery."""
    ctx.remote(settings.REMOTE_UPDATE_SCRIPT)
    ctx.remote('/sbin/service %s restart' % settings.CELERY_SERVICE)


@task
def update_info(ctx):
    """Write info about the current state to a publicly visible file."""
    with ctx.lcd(settings.SRC_DIR):
        ctx.local('date')
        ctx.local('git branch')
        ctx.local('git log -3')
        ctx.local('git status')
        ctx.local('git submodule status')
        ctx.local(PYTHON + ' manage.py migrate --list')
        with ctx.lcd('locale'):
            ctx.local('git branch')
            ctx.local('git log -3')
            ctx.local('git status')

        ctx.local('git rev-parse HEAD > media/revision.txt')


@task
def setup_dependencies(ctx):
    with ctx.lcd(settings.SRC_DIR):
        # Test npm version
        ctx.local('npm -v')

        # Install Node dependencies
        ctx.local('npm install --production --unsafe-perm')


@task
def pre_update(ctx, ref=settings.UPDATE_REF):
    """Update code to pick up changes to this file."""
    update_code(ref)
    setup_dependencies()
    update_info()


@task
def update(ctx):
    update_assets()
    update_product_details()
    update_locales()
    update_db()


@task
def deploy(ctx):
    update_cron()
    checkin_changes()
    deploy_app()
    update_celery()


@task
def update_site(ctx, tag):
    """Update the app to prep for deployment."""
    pre_update(tag)
    update()

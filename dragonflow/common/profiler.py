#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import contextlib

from oslo_log import log as logging

try:
    import osprofiler.initializer
    from osprofiler import opts as profiler_opts
    from osprofiler import profiler
except Exception:
    # osprofiler package is not installed
    profiler_opts = None
    profiler = None

from dragonflow import conf as cfg


CONF = cfg.CONF
if profiler_opts:
    profiler_opts.set_defaults(CONF)
LOG = logging.getLogger(__name__)


@contextlib.contextmanager
def profiler_context(*args, **kwargs):
    if is_profiler_enabled():
        with profiler.Trace(*args, **kwargs) as tracer:
            yield tracer
    else:
        yield None


def _get_profiler_instance():
    # If profiler does not exist or not enabled
    if profiler is None or not CONF.profiler.enabled:
        return None
    instance = profiler.get()
    # Try to initialize an instance
    if instance is None:
        instance = profiler.init(CONF.profiler.hmac_keys)
        LOG.debug("Initialized osprofiler, base trace ID: %s",
                  instance.get_id())
    return instance


def is_profiler_enabled():
    return _get_profiler_instance() is not None


def setup(name, host='0.0.0.0'):
    """Setup OSprofiler notifier and enable profiling.

    :param name: name of the service, that will be profiled
    :param host: host (either host name or host address) the service will be
                 running on. By default host will be set to 0.0.0.0, but more
                 specified host name / address usage is highly recommended.
    """
    if CONF.profiler.enabled:
        osprofiler.initializer.init_from_conf(
            conf=CONF,
            context={},
            project='dragonflow',
            service=name,
            host=host
        )
        LOG.info("OSProfiler is enabled.\n"
                 "Traces provided from the profiler "
                 "can only be subscribed to using the same HMAC keys that "
                 "are configured in Dragonflow's configuration file "
                 "under the [profiler] section.\n To disable OSprofiler "
                 "set in /etc/neutron/dragonflow.conf:\n"
                 "[profiler]\n"
                 "enabled=False")


def get():
    return profiler

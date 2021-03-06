# Copyright (c) 2015 OpenStack Foundation.
#
# All Rights Reserved.
#
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

import copy
import time
import traceback

from jsonmodels import errors
from oslo_config import cfg
from oslo_log import log
from oslo_utils import excutils

import dragonflow.common.exceptions as df_exceptions
from dragonflow.common import utils as df_utils
from dragonflow.db import db_common
from dragonflow.db import model_proxy as mproxy
from dragonflow.db.models import core


LOG = log.getLogger(__name__)
_nb_api = None


def get_db_ip_port():
    hosts = cfg.CONF.df.remote_db_hosts
    if not hosts:
        LOG.warning("Deprecated: remote_db_ip and remote_db_port are "
                    "deprecated for removal. Use remote_db_hosts instead")
        ip = cfg.CONF.df.remote_db_ip
        port = cfg.CONF.df.remote_db_port
        return ip, port
    host = hosts[0]
    ip, port = host.split(':')
    return ip, port


def _get_topic(obj):
    try:
        return getattr(obj, 'topic', None)
    except errors.ValidationError:
        return None


class NbApi(object):

    def __init__(self, db_driver):
        super(NbApi, self).__init__()
        self.driver = db_driver
        self.controller = None
        self.use_pubsub = cfg.CONF.df.enable_df_pub_sub
        self.publisher = None
        self.subscriber = None
        self.enable_selective_topo_dist = \
            cfg.CONF.df.enable_selective_topology_distribution

    @staticmethod
    def get_instance():
        global _nb_api
        if _nb_api is None:
            nb_driver = df_utils.load_driver(
                cfg.CONF.df.nb_db_class,
                df_utils.DF_NB_DB_DRIVER_NAMESPACE)
            nb_api = NbApi(nb_driver)
            ip, port = get_db_ip_port()
            nb_api._initialize(db_ip=ip, db_port=port)
            _nb_api = nb_api
        return _nb_api

    def _initialize(self, db_ip='127.0.0.1', db_port=4001):
        self.driver.initialize(db_ip, db_port, config=cfg.CONF.df)
        if self.use_pubsub:
            self.publisher = self._get_publisher()
            self.subscriber = self._get_subscriber()
            self.publisher.initialize()
            # Start a thread to detect DB failover in Plugin
            self.publisher.set_publisher_for_failover(
                self.publisher,
                self.db_recover_callback)
            self.publisher.start_detect_for_failover()

    def set_db_change_callback(self, db_change_callback):
        if self.use_pubsub:
            # This is here to not allow multiple subscribers to be started
            # under the same process. One should be more than enough.
            if not self.subscriber.is_running:
                self._start_subscriber(db_change_callback)
                # Register for DB Failover detection in NB Plugin
                self.subscriber.set_subscriber_for_failover(
                    self.subscriber,
                    db_change_callback)
                self.subscriber.register_hamsg_for_db()
            else:
                LOG.warning('Subscriber is already initialized, ignoring call')

    def close(self):
        if self.publisher:
            self.publisher.close()
        if self.subscriber:
            self.subscriber.close()

    def db_recover_callback(self):
        # only db with HA func can go in here
        self.driver.process_ha()
        self.publisher.process_ha()
        self.subscriber.process_ha()
        self.controller.sync()

    def _get_publisher(self):
        pub_sub_driver = df_utils.load_driver(
            cfg.CONF.df.pub_sub_driver,
            df_utils.DF_PUBSUB_DRIVER_NAMESPACE)
        return pub_sub_driver.get_publisher()

    def _get_subscriber(self):
        pub_sub_driver = df_utils.load_driver(
            cfg.CONF.df.pub_sub_driver,
            df_utils.DF_PUBSUB_DRIVER_NAMESPACE)
        return pub_sub_driver.get_subscriber()

    def _start_subscriber(self, db_change_callback):
        self.subscriber.initialize(db_change_callback)
        self.subscriber.register_topic(db_common.SEND_ALL_TOPIC)
        publishers_ips = cfg.CONF.df.publishers_ips
        uris = {'%s://%s:%s' % (
                cfg.CONF.df.publisher_transport,
                ip,
                cfg.CONF.df.publisher_port) for ip in publishers_ips}
        publishers = self.get_all(core.Publisher)
        uris |= {publisher.uri for publisher in publishers}
        for uri in uris:
            self.subscriber.register_listen_address(uri)
        self.subscriber.daemonize()

    def support_publish_subscribe(self):
        return self.use_pubsub

    def _send_db_change_event(self, table, key, action, value, topic):
        if not self.use_pubsub:
            return

        if not self.enable_selective_topo_dist or topic is None:
            topic = db_common.SEND_ALL_TOPIC
        update = db_common.DbUpdate(table, key, action, value, topic=topic)
        self.publisher.send_event(update)
        time.sleep(0)

    def register_notification_callback(self, notification_cb):
        self._notification_cb = notification_cb

    def register_listener_callback(self, cb, topic):
        """Register a callback for Neutron listener

        This method is used to register a callback for Neutron listener
        to handle the message from Dragonflow controller. It should only be
        called from Neutron side and only once.

        :param: a callback method to handle the message from Dragonflow
                controller
        :param topic: the topic this neutron listener cares about, e.g the
                      hostname of the node
        """
        if not self.use_pubsub:
            return
        self.subscriber.initialize(cb)
        self.subscriber.register_topic(topic)
        self.subscriber.daemonize()

    def create(self, obj, skip_send_event=False):
        """Create the provided object in the database and publish an event
           about its creation.
        """
        model = type(obj)
        obj.on_create_pre()
        serialized_obj = obj.to_json()
        topic = _get_topic(obj)
        self.driver.create_key(model.table_name, obj.id,
                               serialized_obj, topic)
        if not skip_send_event:
            self._send_db_change_event(model.table_name, obj.id, 'create',
                                       serialized_obj, topic)

    def update(self, obj, skip_send_event=False):
        """Update the provided object in the database and publish an event
           about the change.

           This method reads the existing object from the database and updates
           any non-empty fields of the provided object. Retrieval happens by
           id/topic fields.
        """
        model = type(obj)
        full_obj = self.get(obj)
        db_obj = copy.copy(full_obj)

        if full_obj is None:
            raise df_exceptions.DBKeyNotFound(key=obj.id)

        changed_fields = full_obj.update(obj)

        if not changed_fields:
            return

        full_obj.on_update_pre(db_obj)
        serialized_obj = full_obj.to_json()
        topic = _get_topic(full_obj)

        self.driver.set_key(model.table_name, full_obj.id,
                            serialized_obj, topic)
        if not skip_send_event:
            self._send_db_change_event(model.table_name, full_obj.id, 'set',
                                       serialized_obj, topic)

    def delete(self, obj, skip_send_event=False):
        """Delete the provided object from the database and publish the event
           about its deletion.

           The provided object does not have to have all the fields filled,
           just the ID / topic (if applicable) of the object we wish to delete.
        """
        model = type(obj)
        obj.on_delete_pre()
        topic = _get_topic(obj)
        try:
            self.driver.delete_key(model.table_name, obj.id, topic)
        except df_exceptions.DBKeyNotFound:
            with excutils.save_and_reraise_exception():
                LOG.debug(
                    'Could not find object %(id)s to delete in %(table)s',
                    {'id': obj.id, 'table': model.table_name})

        if not skip_send_event:
            self._send_db_change_event(model.table_name, obj.id, 'delete',
                                       None, topic)

    def get(self, lean_obj):
        """Retrieve a model instance from the database. This function uses
           lean_obj to deduce ID and model type

           >>> nb_api.get(Chassis(id="one"))
           Chassis(id="One", ip="192.168.121.22", tunnel_types=["vxlan"])

        """
        if mproxy.is_model_proxy(lean_obj):
            lean_obj = lean_obj.get_proxied_model()(id=lean_obj.id)
        model = type(lean_obj)
        try:
            serialized_obj = self.driver.get_key(
                model.table_name,
                lean_obj.id,
                _get_topic(lean_obj),
            )
        except df_exceptions.DBKeyNotFound:
            exception_tb = traceback.format_exc()
            LOG.debug(
                'Could not get object %(id)s from table %(table)s',
                {'id': lean_obj.id, 'table': model.table_name})
            LOG.debug('%s', (exception_tb,))
        else:
            return model.from_json(serialized_obj)

    def get_all(self, model, topic=None):
        """Get all instances of provided model, can be limited to instances
           with a specific topic.
        """
        all_values = self.driver.get_all_entries(model.table_name, topic)
        all_objects = [model.from_json(e) for e in all_values]
        return model.on_get_all_post(all_objects)

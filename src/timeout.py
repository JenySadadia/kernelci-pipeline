#!/usr/bin/env python3
#
# SPDX-License-Identifier: LGPL-2.1-or-later
#
# Copyright (C) 2022 Collabora Limited
# Author: Jeny Sadadia <jeny.sadadia@collabora.com>

import sys
from datetime import datetime
from time import sleep
import json
import requests

import kernelci
import kernelci.config
from kernelci.legacy.cli import Args, Command, parse_opts

from base import Service


class TimeoutService(Service):

    def __init__(self, configs, args, name):
        super().__init__(configs, args, name)
        self._pending_states = [
            state.value for state in self._api.node.states
            if state != state.DONE
        ]
        self._user = self._api.user.whoami()
        self._username = self._user['username']

    def _setup(self, args):
        return {
            'poll_period': args.poll_period,
        }

    def _get_pending_nodes(self, filters=None):
        nodes = {}
        node_filters = filters.copy() if filters else {}
        for state in self._pending_states:
            node_filters['state'] = state
            for node in self._api.node.find(node_filters):
                # Until permissions for the timeout service are fixed:
                if node['owner'] == self._username:
                    nodes[node['id']] = node
        return nodes

    def _count_running_child_nodes(self, parent_id, recurse=False):
        nodes_count = 0
        child_nodes = self._api.node.find({
            'parent': parent_id,
        })

        for child in child_nodes:
            if child['state'] in self._pending_states:
                nodes_count += 1
            if recurse:
                nodes_count += self._count_running_child_nodes(child['id'], True)
        return nodes_count

    def _get_child_nodes_recursive(self, node, recursive, state_filter=None):
        child_nodes = self._get_pending_nodes({'parent': node['id']})
        for child_id, child in child_nodes.items():
            if state_filter is None or child['state'] == state_filter:
                recursive.update({child_id: child})
                self._get_child_nodes_recursive(
                    child, recursive, state_filter
                )

    def _submit_lapsed_nodes(self, lapsed_nodes, state, mode):
        for node_id, node in lapsed_nodes.items():
            node_update = node.copy()
            node_update['state'] = state
            self.log.debug(f"{node_id} {mode}")
            if mode == 'TIMEOUT':
                if 'data' not in node_update:
                    node_update['data'] = {}
                if not node.get('result'):
                    # Set result as "incomplete" only if any value is not set yet
                    node_update['result'] = 'incomplete'
                    node_update['data']['error_code'] = 'node_timeout'
                    node_update['data']['error_msg'] = 'Node timed-out'

            try:
                self._api.node.update(node_update)
            except requests.exceptions.HTTPError as err:
                err_msg = json.loads(err.response.content).get("detail", [])
                self.log.error(err_msg)


class Timeout(TimeoutService):

    def __init__(self, configs, args):
        super().__init__(configs, args, 'timeout')

    def _run(self, ctx):
        self.log.info("Looking for nodes with lapsed timeout...")
        self.log.info("Press Ctrl-C to stop.")
        self.log.info(f"Current user: {self._username}")

        while True:
            timeout_nodes = self._get_pending_nodes({
                'timeout__lt': datetime.isoformat(datetime.utcnow())
            })
            self._submit_lapsed_nodes(timeout_nodes, 'done', 'TIMEOUT')
            sleep(ctx['poll_period'])

        return True


class Holdoff(TimeoutService):

    def __init__(self, configs, args):
        super().__init__(configs, args, 'timeout-holdoff')

    def _get_available_nodes(self):
        nodes = self._api.node.find({
            'state': 'available',
            'holdoff__lt': datetime.isoformat(datetime.utcnow()),
        })
        return {node['id']: node for node in nodes}

    def _check_available_nodes(self, available_nodes):
        # Nodes in "available" state with running descendent nodes should
        # transition to "closing" state.
        # If they have no running descendent, then we can directly transition
        # them to "done"
        done_nodes = {}
        closing_nodes = {}
        for node_id, node in available_nodes.items():
            running = self._count_running_child_nodes(node_id)
            self.log.debug(f"{node_id} RUNNING: {running}")
            if not running:
                running = self._count_running_child_nodes(node_id, recurse=True)
                self.log.debug(f"{node_id} RUNNING (recursive): {running}")
            if running:
                closing_nodes[node_id] = node
            else:
                done_nodes[node_id] = node
        self._submit_lapsed_nodes(closing_nodes, 'closing', 'HOLDOFF')
        self._submit_lapsed_nodes(done_nodes, 'done', 'DONE')

    def _run(self, ctx):
        self.log.info("Looking for nodes with lapsed holdoff...")
        self.log.info("Press Ctrl-C to stop.")

        while True:
            available_nodes = self._get_available_nodes()
            self._check_available_nodes(available_nodes)
            sleep(ctx['poll_period'])

        return True


class Closing(TimeoutService):

    def __init__(self, configs, args):
        super().__init__(configs, args, 'timeout-closing')

    def _get_closing_nodes(self):
        nodes = self._api.node.find({'state': 'closing'})
        return {node['id']: node for node in nodes}

    def _check_closing_nodes(self, closing_nodes):
        # Nodes in "closing" state should transition to "done" once they have
        # no descendent node running anymore.
        done_nodes = {}
        for node_id, node in closing_nodes.items():
            running = self._count_running_child_nodes(node_id)
            self.log.debug(f"{node_id} RUNNING: {running}")
            if not running:
                running = self._count_running_child_nodes(node_id, recurse=True)
                self.log.debug(f"{node_id} RUNNING (recursive): {running}")
            if not running:
                done_nodes[node_id] = node
        self._submit_lapsed_nodes(done_nodes, 'done', 'DONE')

    def _run(self, ctx):
        self.log.info("Looking for nodes that are done closing...")
        self.log.info("Press Ctrl-C to stop.")

        while True:
            closing_nodes = self._get_closing_nodes()
            self._check_closing_nodes(closing_nodes)
            sleep(ctx['poll_period'])

        return True


MODES = {
    'timeout': Timeout,
    'holdoff': Holdoff,
    'closing': Closing,
}


class cmd_run(Command):
    help = "Set node state to done if maximum wait time is over"
    args = [
        Args.api_config,
        {
            'name': '--poll-period',
            'type': int,
            'help': "Polling period in seconds",
            'default': 60,
        },
        {
            'name': '--mode',
            'choices': MODES.keys(),
        },
    ]

    def __call__(self, configs, args):
        return MODES[args.mode](configs, args).run(args)


if __name__ == '__main__':
    opts = parse_opts('timeout', globals())
    yaml_configs = opts.get_yaml_configs() or 'config'
    pipeline = kernelci.config.load(yaml_configs)
    status = opts.command(pipeline, opts)
    sys.exit(0 if status is True else 1)

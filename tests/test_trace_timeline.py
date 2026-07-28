# -*- coding: utf-8 -*-
"""Хронология и сквозная цепочка запроса: порядок событий и точка обрыва."""

import json
import unittest

import helpers
from helpers import ScriptCase

SOURCES = ['--log', 'gateway=' + helpers.log('gateway.log'),
           '--log', 'payment=' + helpers.log('payment.log')]
TRACE_ID = 'req-7f3a9c'


class Chain(ScriptCase):

    def test_hops_go_in_order(self):
        chain = self.json_of('trace.py', SOURCES + ['--id', TRACE_ID])
        self.assertEqual([h['service'] for h in chain['hops']], ['gateway', 'payment'])
        self.assertEqual(chain['hops'][0]['first'], '2026-07-28 16:20:01')
        self.assertEqual(chain['records'], 5)

    def test_break_point_is_the_earliest_error(self):
        """Точка обрыва — тот, кто упал первым, а не тот, кто первым в цепочке.

        gateway пишет свой таймаут позже, чем payment — свою ошибку. Разбор
        обязан назвать payment: иначе баг заведут на вызывающий сервис.
        """
        chain = self.json_of('trace.py', SOURCES + ['--id', TRACE_ID])
        self.assertIsNotNone(chain['first_error'])
        self.assertEqual(chain['first_error']['service'], 'payment')
        self.assertEqual(chain['first_error']['ts'], '2026-07-28 16:20:03')
        self.assertIn('ConnectionTimeoutError', chain['first_error']['text'])

    def test_candidates_rank_errors_first(self):
        payload = self.json_of('trace.py', SOURCES + ['--top', '5'])
        self.assertEqual(payload['total_traces'], 2)
        self.assertEqual(payload['candidates'][0]['id'], TRACE_ID)
        self.assertEqual(payload['candidates'][0]['errors'], 2)
        self.assertEqual(payload['candidates'][0]['services'], ['gateway', 'payment'])

    def test_unknown_id_is_reported_without_traceback(self):
        code, out, err = self.run_script('trace.py', SOURCES + ['--id', 'нет-такого'])
        self.assertNotIn('Traceback', err)
        self.assertIn('не найден', out)
        self.assertNotEqual(code, 0, 'ненайденная цепочка должна быть видна по коду возврата')


class Timeline(ScriptCase):

    def setUp(self):
        self.tmp = self.tmpdir()

    def _events(self):
        code, out, err = self.run_script(
            'timeline.py', ['--log', 'payment=' + helpers.log('single_service.log'),
                            '--format', 'json'])
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_first_new_event_is_the_earliest_warning(self):
        events = self._events()
        self.assertTrue(events)
        self.assertEqual(events[0]['ts'], '2026-07-28 12:31:14')
        self.assertEqual(events[0]['kind'], 'первое появление')
        self.assertIn('connection pool exhausted', events[0]['text'])

    def test_events_are_ordered_in_time(self):
        stamps = [e['ts'] for e in self._events()]
        self.assertEqual(stamps, sorted(stamps))

    def test_spike_is_found(self):
        spikes = [e for e in self._events() if e['kind'] == 'всплеск']
        self.assertTrue(spikes, 'всплеск ошибок в 12:34 не найден')
        self.assertEqual(spikes[0]['ts'], '2026-07-28 12:34:00')


if __name__ == '__main__':
    unittest.main()

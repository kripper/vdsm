#
# Copyright 2014 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

from contextlib import contextmanager
import itertools
import os
import tempfile
import random
import shutil
import threading

from vdsm import ipwrapper
from vdsm import libvirtconnection
from vdsm import utils
import virt.sampling as sampling

from testValidation import brokentest, ValidateRunningAsRoot
from testlib import permutations, expandPermutations
from testlib import VdsmTestCase as TestCaseBase
from monkeypatch import MonkeyPatchScope, MonkeyPatch
from functional import dummy


class SamplingTests(TestCaseBase):
    proc_stat_template = """
cpu  4350684 14521 1120299 20687999 677480 197238 48056 0 1383 0
cpu0 1082143 1040 335283 19253788 628168 104752 21570 0 351 0
cpu1 1010362 2065 294113 474697 18915 41743 9793 0 308 0
cpu2 1296289 6812 283613 472725 18664 30549 9776 0 213 0
cpu3 961889 4603 207289 486787 11732 20192 6916 0 511 0
ctxt 690239751
%(btime_line)s
processes 450432
procs_running 2
procs_blocked 0
"""
    fixture_good = proc_stat_template % {'btime_line': 'btime 1395249141'}
    fixture_missing = proc_stat_template % {'btime_line': 'btime'}
    fixture_malformed = proc_stat_template % {'btime_line':
                                              'btime 22not_a_number3'}
    fixture_extra = proc_stat_template % {'btime_line': 'btime 1395249141 foo'}

    def _createFixtureFile(self, name, content):
        path = os.path.join(self._tmpDir, name)
        with open(path, 'w') as f:
            f.write(content)
        return path

    def setUp(self):
        self._tmpDir = tempfile.mkdtemp()
        self._good_path = self._createFixtureFile('good',
                                                  self.fixture_good)
        self._missing_path = self._createFixtureFile('missing',
                                                     self.fixture_missing)
        self._malformed_path = self._createFixtureFile('malformed',
                                                       self.fixture_malformed)
        self._extra_path = self._createFixtureFile('extra',
                                                   self.fixture_extra)

    def tearDown(self):
        shutil.rmtree(self._tmpDir)

    def testBootTimeOk(self):
        with MonkeyPatchScope([(sampling, '_PROC_STAT_PATH',
                                self._good_path)]):
            self.assertEquals(sampling.getBootTime(),
                              1395249141)

    def testBootTimeEmpty(self):
        with MonkeyPatchScope([(sampling, '_PROC_STAT_PATH',
                                '/dev/null')]):
            with self.assertRaises(ValueError):
                sampling.getBootTime()

    def testBootTimeMissing(self):
        with MonkeyPatchScope([(sampling, '_PROC_STAT_PATH',
                                self._missing_path)]):
            with self.assertRaises(ValueError):
                sampling.getBootTime()

    def testBootTimeMalformed(self):
        with MonkeyPatchScope([(sampling, '_PROC_STAT_PATH',
                                self._malformed_path)]):
            with self.assertRaises(ValueError):
                sampling.getBootTime()

    def testBootTimeNonExistantFile(self):
        with MonkeyPatchScope([(sampling, '_PROC_STAT_PATH',
                                '/i/do/not/exist/1234567890')]):
            with self.assertRaises(IOError):
                sampling.getBootTime()

    def testBootTimeExtra(self):
        with MonkeyPatchScope([(sampling, '_PROC_STAT_PATH',
                                self._extra_path)]):
            self.assertEquals(sampling.getBootTime(), 1395249141)


@contextmanager
def dummy_if():
    dummy_name = dummy.create()
    try:
        yield dummy_name
    finally:
        dummy.remove(dummy_name)


@contextmanager
def vlan(name, link, vlan_id):
    ipwrapper.linkAdd(name, 'vlan', link=link, args=['id', str(vlan_id)])
    try:
        yield
    finally:
        try:
            ipwrapper.linkDel(name)
        except ipwrapper.IPRoute2Error:
            # faultyGetLinks is expected to have already removed the vlan
            # device.
            pass


def read_password():
    return 'password'


class InterfaceSampleTests(TestCaseBase):
    def setUp(self):
        self.NEW_VLAN = 'vlan_%s' % (random.randint(0, 1000))

    def testDiff(self):
        lo = ipwrapper.getLink('lo')
        s0 = sampling.InterfaceSample(lo)
        s1 = sampling.InterfaceSample(lo)
        s1.operstate = 'x'
        self.assertEquals('operstate:x', s1.connlog_diff(s0))

    @MonkeyPatch(libvirtconnection, '_read_password', read_password)
    @ValidateRunningAsRoot
    def testHostSampleReportsNewInterface(self):
        hs_before = sampling.HostSample(os.getpid())
        interfaces_before = set(hs_before.interfaces.iterkeys())

        with dummy_if() as dummy_name:
            hs_after = sampling.HostSample(os.getpid())
            interfaces_after = set(hs_after.interfaces.iterkeys())
            interfaces_diff = interfaces_after - interfaces_before
            self.assertEqual(interfaces_diff, set([dummy_name]))

    @MonkeyPatch(libvirtconnection, '_read_password', read_password)
    @ValidateRunningAsRoot
    def testHostSampleHandlesDisappearingVlanInterfaces(self):
        original_getLinks = ipwrapper.getLinks

        def faultyGetLinks():
            all_links = list(original_getLinks())
            ipwrapper.linkDel(self.NEW_VLAN)
            return iter(all_links)

        with MonkeyPatchScope(
                [(ipwrapper, 'getLinks', faultyGetLinks)]):
            with dummy_if() as dummy_name:
                with vlan(self.NEW_VLAN, dummy_name, 999):
                    hs = sampling.HostSample(os.getpid())
                    self.assertNotIn(self.NEW_VLAN, hs.interfaces)


@expandPermutations
class SampleWindowTests(TestCaseBase):
    _VALUES = (19, 42, 23)  # throwaway values, no meaning

    def setUp(self):
        self._counter = itertools.count(0)
        self.win = sampling.SampleWindow(
            size=2, timefn=lambda: next(self._counter))

    @permutations([[-1], [0]])
    def test_window_size_bad_values(self, size):
        self.assertRaises(
            ValueError,
            sampling.SampleWindow, size)

    def test_last(self):
        win = sampling.SampleWindow(size=2)
        win.append(self._VALUES[0])
        win.append(self._VALUES[1])
        self.assertEqual(self._VALUES[1], win.last())

    def test_stats_empty(self):
        self.assertEqual(self.win.stats(), (None, None, None))

    def test_stats_one_value(self):
        self.win.append(self._VALUES[0])
        self.assertEqual(self.win.stats(), (None, None, None))

    def test_stats_two_values(self):
        for val in self._VALUES:
            self.win.append(val)
        self.assertEqual(self.win.stats(),
                         (self._VALUES[-2], self._VALUES[-1], 1))


@expandPermutations
class AdvancedStatsFunctionTests(TestCaseBase):
    @permutations([[None], [-1], [0], [1.333], ['foo']])
    def testIntervalBadValues(self, interval):
        self.assertRaises(
            ValueError,
            sampling.AdvancedStatsFunction, lambda x: x, interval)

    def testIntervalGoodValue(self):
        interval = 42
        stat = sampling.AdvancedStatsFunction(random.randint, interval)
        self.assertEqual(stat.interval, interval)

    def testCall(self):
        value = 42
        stat = sampling.AdvancedStatsFunction(lambda x: x, interval=1)
        ret = stat(value)
        self.assertEqual(ret, value)

    def testWindowSizeOne(self):
        value = 42
        stat = sampling.AdvancedStatsFunction(
            lambda x: x, interval=1, window=1)
        stat(value)
        self.assertEqual(stat.getStats(), (None, None, None))
        self.assertEqual(stat.getLastSample(), value)

    def testGetStats(self):
        values = range(42)
        stat = sampling.AdvancedStatsFunction(
            lambda x: x, interval=1, window=2)
        for val in values:
            stat(val)
        bgn, end, diff = stat.getStats()
        self.assertEqual(bgn, values[-2])
        self.assertEqual(end, values[-1])

    def testElapsedTime(self):
        counter = itertools.count()
        stat = sampling.AdvancedStatsFunction(
            lambda x: x, interval=1, window=2, timefn=lambda: next(counter))
        for val in range(42):
            stat(val)
        bgn, end, diff = stat.getStats()
        self.assertTrue(diff > 0)  # assertGreater requires py >= 2.7

    def testLastSample(self):
        values = range(42)
        stat = sampling.AdvancedStatsFunction(
            lambda x: x, interval=1, window=2)
        for val in values:
            stat(val)
        self.assertEqual(stat.getLastSample(), values[-1])


class HostStatsThread(TestCaseBase):
    FAILED_SAMPLE = 3  # random 'small' value
    STOP_SAMPLE = 6  # ditto

    def setUp(self):
        self._hs = None
        self._sampleCount = 0
        self._samplingDone = threading.Event()

    @brokentest
    def testContinueWithErrors(self):
        """
        bz1113948: do not give up on errors != TimeoutError
        """
        def WrapHostSample(pid):
            self._sampleCount += 1
            if self._sampleCount == self.FAILED_SAMPLE:
                raise ValueError
            if self._sampleCount == self.STOP_SAMPLE:
                self._hs.stop()
                self._samplingDone.set()
            return sampling.HostSample(1)

        with MonkeyPatchScope([(sampling, 'HostSample', WrapHostSample),
                               (sampling.HostStatsThread,
                                   'SAMPLE_INTERVAL_SEC', 0.1)]):
            self._hs = sampling.HostStatsThread(self.log)
            self._hs.start()
            self._samplingDone.wait(3.0)
            self.assertTrue(self._samplingDone.is_set())
            self.assertTrue(self._sampleCount >= self.STOP_SAMPLE)


class StatsCacheTests(TestCaseBase):

    FAKE_CLOCK_STEP = 1

    def setUp(self):
        self.clock = 0
        with MonkeyPatchScope([(utils, 'monotonic_time',
                                self.fake_monotonic_time)]):
            self.cache = sampling.StatsCache()

    def fake_monotonic_time(self):
        self.clock += self.FAKE_CLOCK_STEP
        return self.clock

    def test_empty(self):
        res = self.cache.get('x')  # vmid not relevant
        self.assertEqual(res, (None, None, None))

    def test_not_enough_samples(self):
        self._feed_cache((
            ({'a': 42}, 1),
        ))
        res = self.cache.get('a')
        self.assertEqual(res, (None, None, None))

    def test_get(self):
        self._feed_cache((
            ({'a': 'foo'}, 1),
            ({'a': 'bar'}, 2)
        ))
        res = self.cache.get('a')
        self.assertEqual(res, ('foo', 'bar', self.FAKE_CLOCK_STEP))

    def test_get_missing(self):
        self._feed_cache((
            ({'a': 'foo'}, 1),
            ({'a': 'bar'}, 2)
        ))
        res = self.cache.get('b')
        self.assertEqual(res, (None, None, None))

    def test_put_overwrite(self):
        self._feed_cache((
            ({'a': 'foo'}, 1),
            ({'a': 'bar'}, 2),
            ({'a': 'baz'}, 3)
        ))
        res = self.cache.get('a')
        self.assertEqual(res, ('bar', 'baz', self.FAKE_CLOCK_STEP))

    def test_put_out_of_order(self):
        self._feed_cache((
            ({'a': 'foo'}, 10),
            ({'a': 'bar'},  0),
            ({'a': 'baz'}, 30)
        ))
        res = self.cache.get('a')
        self.assertEqual(res, ('foo', 'baz', self.FAKE_CLOCK_STEP))
        # interval is computed using fake_monotonic_time, not the
        # given timestamp, which is used only to enforce ordering.

    def test_skip_one_cycle(self):
        # as unfortunate side effect, there is room only for
        # last two _global_ samples (not per-vm)
        self._feed_cache((
            ({'a': 'foo', 'b': 'foo'}, 1),
            ({'a': 'bar'}, 2),
            # here we lost sampling for 'b'
            ({'a': 'baz', 'b': 'baz'}, 3),
        ))
        self.assertEqual(self.cache.get('a'), ('bar', 'baz', 1))
        self.assertEqual(self.cache.get('b'), (None, None, None))

    def _feed_cache(self, samples):
        for sample in samples:
            self.cache.put(*sample)

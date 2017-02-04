import subprocess
import common
import settings
import monitoring
import os
import time
import threading
import logging
import re

from cluster.ceph import Ceph
from benchmark import Benchmark

logger = logging.getLogger("cbt")


class Getput(Benchmark):

    def __init__(self, cluster, config):
        super(Getput, self).__init__(cluster, config)

        self.tmp_conf = self.cluster.tmp_conf
        self.runtime =  config.get('runtime', '300')
        self.container_prefix = config.get('container_prefix', None)
        self.object_prefix = config.get('object_prefix', None)
        self.concurrent_procs = config.get('concurrent_procs', 1)
        self.ops_per_proc = config.get('ops_per_proc', None)
        self.tests = config.get('tests', "p")
        self.op_size = config.get('op_size', 4194304)
        self.ctype = config.get('ctype', None)
        self.run_dir = '%s/osd_ra-%08d/op_size-%08d/concurrent_procs-%08d/%s' % (self.run_dir, int(self.osd_ra), int(self.op_size), int(self.concurrent_procs), self.tests)
        self.out_dir = '%s/osd_ra-%08d/op_size-%08d/concurrent_procs-%08d/%s' % (self.archive_dir, int(self.osd_ra), int(self.op_size), int(self.concurrent_procs), self.tests)
        self.pool_profile = config.get('pool_profile', 'default')
        self.cmd_path = config.get('cmd_path', "/usr/bin/getput")
        self.user = config.get('user', 'cbt')
        self.key = config.get('key', 'cbt')
        self.auth_urls = config.get('auth', self.cluster.get_auth_urls())

    def exists(self):
        if os.path.exists(self.out_dir):
            logger.info('Skipping existing test in %s.', self.out_dir)
            return True
        return False

    # Initialize may only be called once depending on rebuild_every_test setting
    def initialize(self): 
        super(Getput, self).initialize()

        # create the user and key
        self.cluster.add_swift_user(self.user, self.key)


        # Clean and Create the run directory
        common.clean_remote_dir(self.run_dir)
        common.make_remote_dir(self.run_dir)

        logger.info('Running scrub monitoring.')
        monitoring.start("%s/scrub_monitoring" % self.run_dir)
        self.cluster.check_scrub()
        monitoring.stop()

        logger.info('Pausing for 60s for idle monitoring.')
        monitoring.start("%s/idle_monitoring" % self.run_dir)
        time.sleep(60)
        monitoring.stop()

        common.sync_files('%s/*' % self.run_dir, self.out_dir)

        return True

    def mkcredfiles(self):
        for i in xrange(0, len(self.auth_urls)):
            cred = "export ST_AUTH=%s\nexport ST_USER=%s\nexport ST_KEY=%s" % (self.auth_urls[i], self.user, self.key)
            common.pdsh(settings.getnodes('clients'), 'echo -e "%s" >> %s/gw%02d.cred' % (cred, self.run_dir, i)).communicate()

    def mkgetputcmd(self, cred_file):
        # grab the executable to use
        getput_cmd = '%s ' % self.cmd_path

        # Set the options        
        if self.container_prefix is not None:
            getput_cmd += '-c%s ' % self.container_prefix
        if self.object_prefix is not None:
            getput_cmd += '-o%s ' % self.object_prefix
        getput_cmd += '-s%s ' % self.op_size
        getput_cmd += '-t%s ' % self.tests
        getput_cmd += '--concurrent_procs %s ' % self.concurrent_procs
        if self.ops_per_proc is not None:
            getput_cmd += '-n%s ' % self.ops_per_proc
        if self.runtime is not None:
            getput_cmd += '--runtime %s ' % self.runtime
        if self.ctype is not None:
            getput_cmd += '--ctype %s ' % self.ctype
        getput_cmd += '--cred %s' % cred_file

        # End the getput_cmd
        getput_cmd += '> %s/output' % self.run_dir

        return getput_cmd

    def run(self):
        # First create a credential file for each gateway
        self.mkcredfiles()

        # We'll always drop caches for rados bench
        self.dropcaches()
        
        # dump the cluster config
        self.cluster.dump_config(self.run_dir)

        # Run the backfill testing thread if requested
        if 'recovery_test' in self.cluster.config:
            recovery_callback = self.recovery_callback
            self.cluster.create_recovery_test(self.run_dir, recovery_callback)

        # Run getput 
        monitoring.start(self.run_dir)
        logger.info('Running getput %s test.' % self.tests)

        ps = []
        for i in xrange(0, len(self.auth_urls)):
            cmd = self.mkgetputcmd("%s/gw%02d.cred" % (self.run_dir, i))
            p = common.pdsh(settings.getnodes('clients'), cmd)
            ps.append(p)
        for p in ps:
            p.wait()
        monitoring.stop(self.run_dir)

        # If we were doing recovery, wait until it's done.
        if 'recovery_test' in self.cluster.config:
            self.cluster.wait_recovery_done()

        # Finally, get the historic ops
        self.cluster.dump_historic_ops(run_dir)
        common.sync_files('%s/*' % self.run_dir, self.out_dir)

    def recovery_callback(self): 
        common.pdsh(settings.getnodes('clients'), 'sudo killall -9 getput').communicate()

    def __str__(self):
        return "%s\n%s\n%s" % (self.run_dir, self.out_dir, super(Getput, self).__str__())
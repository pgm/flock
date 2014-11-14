import subprocess
import time
import threading
import Queue
import traceback
from instance_types import cpus_per_instance
import term

class Parameters:
    def __init__(self):
        self.is_paused=True
        self.interval=30
        self.spot_bid=0.01
        self.max_to_add=1
        self.time_per_job=30 * 60
        self.time_to_add_servers_fixed=60
        self.time_to_add_servers_per_server=30
        self.max_instances=10
        self.instance_type="m3.medium"
        self.domain="cluster-deadmans-switch"
        self.dryrun=False
        self.jobs_per_server=1
        self.log_file = None

    def generate_args(self):
        cpus = cpus_per_instance[self.instance_type]

        return ["--spot_bid", str(self.spot_bid * cpus),
                "--max_to_add", str(self.max_to_add),
                "--time_per_job", str(self.time_per_job),
                "--time_to_add_servers_fixed", str(self.time_to_add_servers_fixed),
                "--time_to_add_servers_per_server", str(self.time_to_add_servers_per_server),
                "--instance_type", str(self.instance_type),
                "--domain", self.domain,
                "--jobs_per_server", str(self.jobs_per_server),
                "--logfile", self.log_file,
                "--max_instances", str(self.max_instances)
                ]



# different states the cluster can be in
CM_STOPPED = "stopped"
CM_STARTING = "starting"
CM_UPDATING = "updating"
CM_SLEEPING = "sleeping"
CM_STOPPING = "stopping"
CM_DEAD = "dead"

class ClusterManager(object):
    def __init__(self, monitor_parameters, cluster_name, cluster_template, terminal, cmd_prefix, clusterui_identifier, ec2):
        super(ClusterManager, self).__init__()
        self.state = CM_DEAD
        self.requested_stop = False
        self.monitor_parameters = monitor_parameters
        self.cluster_name = cluster_name
        self.terminal = terminal
        self.queue = Queue.Queue()
        self.cmd_prefix = cmd_prefix
        self.clusterui_identifier = clusterui_identifier
        self.ec2 = ec2
        self.cluster_template = cluster_template
        self.first_update = True
        self.thread = None

    def _send_wakeup(self):
        self.tell("update")

    def start_manager(self):
        # make sure we don't try to have two running manager threads
        assert self.thread is None or not self.thread.is_alive

        # find out if cluster is already running
        process = subprocess.Popen(self.cmd_prefix+["listclusters", self.cluster_name], stderr=subprocess.PIPE, stdout=subprocess.PIPE)
        output, stderr = process.communicate()

        print "output: %s, stderr=%s" % (repr(output), repr(stderr))

        if "does not exist" in stderr:
            cluster_is_running = False
            self.state = CM_STOPPED
        else:
            assert "security group" in output
            cluster_is_running = True
            self.state = CM_STARTING
            self.first_update = True

        self.thread = threading.Thread(target=self.run)
        self.thread.daemon = True
        self.thread.start()

        if cluster_is_running:
            self.tell("start-completed")

    def start_cluster(self):
        self.tell("start")

    def stop_cluster(self):
        self.tell("stop")

    def run(self):
        running = True

        # if handle each message from the queue, but if we get an exception,
        # let the thread die after writing out the exception to the terminal
        while running:
            try:
                message = self.queue.get()
                self.on_receive(message)
            except:
                exception_message = traceback.format_exc()

                print(exception_message)
                self.terminal.write(exception_message)

                running = False

        self.state = CM_DEAD

    def tell(self, msg):
        self.queue.put(msg)

    def get_state(self):
        return self.state

    def _run_cmd(self, args, post_execute_msg):
        print "executing %s" % args
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True)
        mp = term.ManagedProcess(p, p.stdout, self.terminal)
        completion = mp.start_thread()
        completion.then(lambda: self.tell(post_execute_msg))

    def _run_starcluster_cmd(self, args, post_execute_msg):
        self._run_cmd(self.cmd_prefix + args, post_execute_msg)

    def _execute_shutdown(self):
        self._run_starcluster_cmd(["terminate", "--confirm", self.cluster_name], "stop-completed")
        self.state = CM_STOPPING

    def _execute_startup(self):
        cmd, flag, config = self.cmd_prefix
        assert flag == "-c"
        self._run_cmd(["./start_cluster.sh", cmd, config, self.cluster_name, self.cluster_template], "start-completed")
        self.state = CM_STARTING

    def _execute_sleep_then_poll(self):
        self.state = CM_SLEEPING
        sleep_timer = threading.Timer(self.monitor_parameters.interval, self._send_wakeup)
        sleep_timer.start()

    def _verify_ownership_of_cluster(self, steal_ownership=False):
        security_group_name = "@sc-%s" % self.cluster_name
        security_groups = self.ec2.get_all_security_groups([security_group_name])
        #if len(security_groups) == 0:
        #    return

        security_group_id = security_groups[0].id

        tags = self.ec2.get_all_tags(filters={"resource-id": security_group_id, "key": "clusterui-instance"})
        if len(tags) == 0 or steal_ownership:
            self.ec2.create_tags([security_group_id], {"clusterui-instance": self.clusterui_identifier})
        else:
            assert len(tags) == 1
            tag = tags[0]
            if tag.value != self.clusterui_identifier:
                self.state = "broken-lost-ownership"
                raise Exception("Expected ownership tag to be %s but was %s" % (repr(self.clusterui_identifier, tag.value)))

    def _execute_poll(self):
        self.state = CM_UPDATING
        if not self.monitor_parameters.is_paused:
            print "updating"
            self._verify_ownership_of_cluster(steal_ownership=self.first_update)
            self.first_update = False

            args = self.monitor_parameters.generate_args()
            self._run_starcluster_cmd(["scalecluster", self.cluster_name] + args, "update-completed")
        else:
            print "is paused"
            self.tell("update-completed")

    def on_receive(self, message):
        if isinstance(message, dict):
            cmd = message['command']
        else:
            cmd = message

        print "on_receive(%s)" % cmd

        if cmd == "state?":
            return self.state
        elif cmd == "start":
            if self.state == CM_STOPPED or self.state == CM_DEAD:
                self._execute_startup()
        elif cmd == "stop":
            if self.state in [CM_STARTING, CM_STOPPING, CM_UPDATING]:
                self.requested_stop = True
            elif self.state in [CM_SLEEPING, CM_STOPPED]:
                self._execute_shutdown()
            else:
                print "stop but state = %s" % self.state
        elif cmd == "update":
            if self.state == CM_SLEEPING:
                self._execute_poll()
        elif cmd == "update-completed":
            if self.state == CM_UPDATING:
                self._execute_sleep_then_poll()
        elif cmd == "start-completed":
            if self.state == CM_STARTING:
                self._execute_sleep_then_poll()
        elif cmd == "stop-completed":
            if self.state == CM_STOPPING:
                self.state = CM_STOPPED
        else:
            print "Unhandled cmd: %s!!!!" % cmd

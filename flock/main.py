import os
import sys
import argparse
import __init__ as flock
import shutil
import logging
__author__ = 'pmontgom'

log = logging.getLogger("flock")


def main(cmd_line_args):
    FORMAT = "[%(asctime)-15s] %(message)s"
    logging.basicConfig(format=FORMAT, level=logging.INFO, datefmt="%Y%m%d-%H%M%S")

    flock_home = os.path.dirname(os.path.realpath(__file__))

    parser = argparse.ArgumentParser()
    parser.add_argument('--nowait', help='foo help', action='store_true')
    parser.add_argument('--test', help='Run a test job', action='store_true')
    parser.add_argument('--maxsubmit', type=int, default=1000000)
    parser.add_argument('--rundir', help="Override the run directory used by this run")
    parser.add_argument('--workdir', help="Override the working directory used by each task")
    parser.add_argument('--executor', help="Override the execution method")
    parser.add_argument("--monitor")
    parser.add_argument('command', help='One of: run, check, poll, retry or kill')
    parser.add_argument('run_id', help='Path to config file, which in turn will be used as the id for this run')

    args = parser.parse_args(cmd_line_args)

    # load the config files
    config_files = []

    flock_default_config = os.path.expanduser("~/.flock")
    if os.path.exists(flock_default_config):
        config_files.append(flock_default_config)
    config_files.append(args.run_id)

    overrides = {}
    if args.rundir:
        overrides['run_id'] = args.rundir
    if args.workdir:
        overrides['workdir'] = args.workdir
    if args.executor:
        overrides["executor"] = args.executor
    if args.monitor:
        overrides["monitor_port"] = args.monitor

    config = flock.load_config(config_files, args.run_id, overrides)

    run_id = config.run_id

    if config.monitor_port != None:
        listener = flock.ConsolidatedMonitor(int(config.monitor_port), flock_home)
    else:
        listener = flock.JobListener()

    # now, interpret that config
    if config.executor == "localbg":
        job_queue = flock.LocalBgQueue(listener, config.workdir)
    elif config.executor == "local":
        job_queue = flock.LocalQueue(listener, config.workdir)
    elif config.executor == "sge":
        job_queue = flock.SGEQueue(listener, config.qsub_options, config.scatter_qsub_options, config.name, config.workdir)
    elif config.executor == "lsf":
        job_queue = flock.LSFQueue(listener, config.bsub_options, config.scatter_bsub_options, config.workdir)
    else:
        raise Exception("Unknown executor: %s" % config.executor)

    command = args.command

    test_job_count = None
    if args.test:
        test_job_count = 5
        run_id += "-test"

    log.info("Writing run to \"%s\"", run_id)

    f = flock.Flock(job_queue, flock_home)
    job_queue.system = f.system

    if command == "run":
        if args.test and os.path.exists(run_id):
            log.warn("%s already exists -- removing before running job", run_id)
            shutil.rmtree(run_id)

        f.run(run_id, config.invoke, not args.nowait, args.maxsubmit, test_job_count)
    elif command == "kill":
        f.kill(run_id)
    elif command == "check":
        f.check_and_print(run_id)
    elif command == "poll":
        f.poll(run_id, not args.nowait, args.maxsubmit)
    elif command == "retry":
        f.retry(run_id, not args.nowait, args.maxsubmit)
    elif command == "failed":
        f.list_failures(run_id)
    else:
        raise Exception("Unknown command: %s" % command)

if __name__ == "__main__":
    main(sys.argv[1:])

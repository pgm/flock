import sys
from fabric.api import run, local, put, get, cd, settings
import fabric.contrib.files
import fabric.network
import tempfile
import logging
import json
import sshxmlrpc
import uuid

logging.basicConfig(level=logging.WARN)
log = logging.getLogger("remoteExec")

def exists(path, verbose=False):
    return run("/usr/bin/test -e %s" % path, warn_only=True, quiet=True).return_code == 0

def get_random_id():
    return str(uuid.uuid4())

def deploy_code_from_git(code_dir, repo, sha, branch):
    sha_code_dir = code_dir+"/"+sha
    temp_code_dir = code_dir+"/.temp-"+get_random_id()
    if not exists(sha_code_dir, verbose=True):
        log.info("Deploying code to %s" % sha_code_dir)

        # construct zip file from git and copy to host
        with tempfile.NamedTemporaryFile() as zip_temp_file:
            zip_temp_file_name = zip_temp_file.name
            # it makes me sad that I can't archive by sha, so there's a race condition here.  The solution appears to be
            # to locally mirror the repo, but this such a small volume project, I'll delay implementing that.
            local("git archive --remote "+repo+" -o "+zip_temp_file_name+" --format=zip "+branch)
    
            target_zip_file = code_dir+"/"+sha+".zip"
            put(zip_temp_file_name, target_zip_file)

        # create target directory where the code will live
        run("mkdir -p "+temp_code_dir)

        # extract the file into the code directory and clean up            
        with cd(temp_code_dir):
            run("unzip "+target_zip_file)

        run("mv "+temp_code_dir+" "+sha_code_dir)
        run("rm "+target_zip_file)
    else:
        log.warn("Code already deployed to %s, skipping deploy" % sha_code_dir)

    return sha_code_dir

def install_config(target_root, config_temp_file, timestamp):
    target_dir = target_root+"/"+timestamp

    # create the directory for this run
    run("mkdir -p "+target_dir)

    # upload the config file and run via flock, after setting the working directory to be the current code dir
    remote_config = target_dir+"/config"
    put(config_temp_file, remote_config)

    return target_dir, remote_config

def install_wrapper_script(sha_code_dir, target_dir, flock_path):
    with tempfile.NamedTemporaryFile() as temp_file:
        temp_file_name = temp_file.name
        temp_file.write("#!/bin/bash\n")
        temp_file.write("cd %s\n" % sha_code_dir)
        temp_file.write("echo retrying... >> "+target_dir+"/output.txt\n")
        temp_file.write(flock_path+" --rundir "+target_dir+"/files $* "+target_dir+"/config 2>&1 | tee -a "+target_dir+"/output.txt\n")
        temp_file.flush()

        target_script = target_dir+"/flock-wrapper.sh"
        put(temp_file_name, target_script)

class EchoAndCapture(object):
    def __init__(self, filename):
        self.f = open(filename, "w")

    def write(self, buffer):
        sys.stdout.write(buffer)
        return self.f.write(buffer)

    def flush(self):
        self.f.flush()

    def close(self):
        self.f.close()

def transfer_config_and_submit(host, key_filename, config_file, runs_dir, json_params, timestamp):
    params = json.loads(open(json_params).read())
    with settings(host_string=host, key_filename=key_filename, user="ubuntu"):
        target_dir, remote_config = install_config(runs_dir, config_file, timestamp)
    submit_to_wingman(host, key_filename, target_dir, timestamp, remote_config, params)

def submit_to_wingman(host, key_filename, target_dir, timestamp, remote_config, params):
    service = sshxmlrpc.SshXmlServiceProxy(host, "ubuntu", key_filename, 3010, ["run_submitted"])
    service.run_submitted(target_dir+"/files", timestamp, remote_config, json.dumps(params))

def deploy(host, key_filename, repo, branch, config_file, target_root, json_params, timestamp, flock_path, sha):
    with settings(host_string=host, key_filename=key_filename, user="root"):
        sha_code_dir = deploy_code_from_git(target_root+"/code_cache", repo, sha, branch)

        params = json.loads(open(json_params).read())
        params["sha"] = sha
        with open(json_params, "w") as fd:
            fd.write(json.dumps(params))

    with settings(host_string=host, key_filename=key_filename, user="ubuntu"):
        working_dir = sha_code_dir
        target_dir, remote_config = install_config(target_root, config_file, timestamp)

        put(json_params, target_dir+"/config.json")
        with cd(working_dir):
            install_wrapper_script(working_dir, target_dir, flock_path)

        submit_to_wingman(host, key_filename, target_dir, timestamp, remote_config, params)

if __name__ == "__main__":
    try:
        if sys.argv[1] == "exec-only":
            transfer_config_and_submit(*sys.argv[2:])
        else:
            deploy(*sys.argv[1:])
    finally:
        fabric.network.disconnect_all()



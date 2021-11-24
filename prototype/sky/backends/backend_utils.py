"""Util constants/functions for the backends."""
import datetime
import io
import os
import selectors
import subprocess
import tempfile
import textwrap
import time
from typing import List, Optional, Union
import yaml
import zlib

import jinja2

from sky import authentication as auth
from sky import clouds
from sky import logging
from sky import task

logger = logging.init_logger(__name__)

# An application.  These are the task types to support.
App = Union[task.Task, task.ParTask]
RunId = str
# NOTE: keep in sync with the cluster template 'file_mounts'.
SKY_REMOTE_WORKDIR = '/tmp/workdir'
IP_ADDR_REGEX = r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}'
SKY_LOGS_DIRECTORY = './sky_logs'


def _get_rel_path(path: str) -> str:
    cwd = os.getcwd()
    common = os.path.commonpath([path, cwd])
    return os.path.relpath(path, common)


def _fill_template(template_path: str,
                   variables: dict,
                   output_path: Optional[str] = None) -> str:
    """Create a file from a Jinja template and return the filename."""
    assert template_path.endswith('.j2'), template_path
    with open(template_path) as fin:
        template = fin.read()
    template = jinja2.Template(template)
    content = template.render(**variables)
    if output_path is None:
        output_path, _ = template_path.rsplit('.', 1)
    with open(output_path, 'w') as fout:
        fout.write(content)
    logger.info(f'Created or updated file {_get_rel_path(output_path)}')
    return output_path


def write_cluster_config(run_id: RunId,
                         task: task.Task,
                         cluster_config_template: str,
                         region: Optional[clouds.Region] = None,
                         zones: Optional[List[clouds.Zone]] = None,
                         dryrun: bool = False):
    """Fills in cluster configuration templates and writes them out.

    Returns: {provisioner: path to yaml, the provisioning spec}.
      'provisioner' can be
        - 'ray'
        - 'gcloud' (if TPU is requested)
    """
    cloud = task.best_resources.cloud
    resources_vars = cloud.make_deploy_resources_variables(task)
    config_dict = {}

    if region is None:
        assert zones is None, 'Set either both or neither for: region, zones.'
        region = cloud.get_default_region()
        zones = region.zones
    else:
        assert zones is not None, \
            'Set either both or neither for: region, zones.'
    region = region.name
    if isinstance(cloud, clouds.AWS):
        # Only AWS supports multiple zones in the 'availability_zone' field.
        zones = [zone.name for zone in zones]
    else:
        zones = [zones[0].name]

    aws_default_ami = None
    if isinstance(cloud, clouds.AWS):
        aws_default_ami = cloud.get_default_ami(region)

    setup_sh_path = None
    if task.setup is not None:
        codegen = textwrap.dedent(f"""#!/bin/bash
            . $(conda info --base)/etc/profile.d/conda.sh
            {task.setup}
        """)
        # Use a stable path, /<tempdir>/sky_setup_<checksum>.sh, because
        # rerunning the same task without any changes to the content of the
        # setup command should skip the setup step.  Using NamedTemporaryFile()
        # would generate a random path every time, hence re-triggering setup.
        checksum = zlib.crc32(codegen.encode())
        tempdir = tempfile.gettempdir()
        # TODO: file lock on this path, in case tasks have the same setup cmd.
        with open(os.path.join(tempdir, f'sky_setup_{checksum}.sh'), 'w') as f:
            f.write(codegen)
        setup_sh_path = f.name

    yaml_path = _fill_template(
        cluster_config_template,
        dict(
            resources_vars,
            **{
                'run_id': run_id,
                'setup_sh_path': setup_sh_path,
                'workdir': task.workdir,
                'docker_image': task.docker_image,
                'container_name': task.container_name,
                'num_nodes': task.num_nodes,
                'file_mounts': task.get_local_to_remote_file_mounts() or {},
                # Region/zones.
                'region': region,
                'zones': ','.join(zones),
                # AWS only.
                'aws_default_ami': aws_default_ami,
            }))
    config_dict['ray'] = yaml_path
    if dryrun:
        return config_dict
    _add_ssh_to_cluster_config(cloud, yaml_path)
    if resources_vars.get('tpu_type') is not None:
        # FIXME: replace hard-coding paths
        config_dict['gcloud'] = (_fill_template(
            'config/gcp-tpu-create.sh.j2',
            dict(resources_vars, **{
                'zones': ','.join(zones),
            })),
                                 _fill_template(
                                     'config/gcp-tpu-delete.sh.j2',
                                     dict(resources_vars, **{
                                         'zones': ','.join(zones),
                                     })))
    return config_dict


def _add_ssh_to_cluster_config(cloud_type, cluster_config_file):
    """Adds SSH key info to the cluster config.

    This function's output removes comments included in the jinja2 template.
    """
    with open(cluster_config_file, 'r') as f:
        config = yaml.safe_load(f)
    cloud_type = str(cloud_type)
    if cloud_type == 'AWS':
        config = auth.setup_aws_authentication(config)
    elif cloud_type == 'GCP':
        config = auth.setup_gcp_authentication(config)
    elif cloud_type == 'Azure':
        config = auth.setup_azure_authentication(config)
    else:
        raise ValueError('Cloud type not supported, must be [AWS, GCP, Azure]')
    yaml_dump(cluster_config_file, config)


def yaml_dump(path, config):
    # https://github.com/yaml/pyyaml/issues/127
    class LineBreakDumper(yaml.SafeDumper):

        def write_line_break(self, data=None):
            super().write_line_break(data)
            if len(self.indents) == 1:
                super().write_line_break()

    with open(path, 'w') as f:
        yaml.dump(config,
                  f,
                  Dumper=LineBreakDumper,
                  sort_keys=False,
                  default_flow_style=False)


def get_run_id() -> RunId:
    return 'sky-' + datetime.datetime.now().strftime('%Y-%m-%d-%H-%M-%S-%f')


def wait_until_ray_cluster_ready(cloud: clouds.Cloud, cluster_config_file: str,
                                 num_nodes: int):
    if num_nodes <= 1:
        return
    expected_worker_count = num_nodes - 1
    if isinstance(cloud, clouds.AWS):
        worker_str = 'ray.worker.default'
    elif isinstance(cloud, clouds.GCP):
        worker_str = 'ray_worker_default'
    else:
        assert False, f'No support for distributed clusters for {cloud}.'
    while True:
        proc = subprocess.run(f"ray exec {cluster_config_file} 'ray status'",
                              shell=True,
                              check=True,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE)
        output = proc.stdout.decode('ascii')
        logger.info(output)
        if f'{expected_worker_count} {worker_str}' in output:
            break
        time.sleep(10)


def run_command_on_ip_via_ssh(ip: str,
                              command: str,
                              private_key: str,
                              container_name: Optional[str],
                              user: str = 'ubuntu') -> None:
    if container_name is not None:
        command = command.replace('\\', '\\\\').replace('"', '\\"')
        command = f'docker exec {container_name} /bin/bash -c "{command}"'
    cmd = [
        'ssh',
        '-i',
        private_key,
        '-o',
        'StrictHostKeyChecking=no',
        '{}@{}'.format(user, ip),
        command  # TODO: shlex.quote() doesn't work.  Is it needed in a list?
    ]
    proc = subprocess.Popen(cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            universal_newlines=True)
    outs, errs = proc.communicate()
    if outs:
        logger.info(outs)
    if proc.returncode:
        if errs:
            logger.error(errs)
        raise subprocess.CalledProcessError(proc.returncode, cmd)


def redirect_process_output(proc, log_path, stream_logs, start_streaming_at=''):
    """Redirect the process's filtered stdout/stderr to both stream and file"""
    dirname = os.path.dirname(log_path)
    os.makedirs(dirname, exist_ok=True)

    out_io = io.TextIOWrapper(proc.stdout, encoding='utf-8', newline='')
    err_io = io.TextIOWrapper(proc.stderr, encoding='utf-8', newline='')
    sel = selectors.DefaultSelector()
    sel.register(out_io, selectors.EVENT_READ)
    sel.register(err_io, selectors.EVENT_READ)

    stdout = ''
    stderr = ''

    start_streaming_flag = False
    with open(log_path, 'a') as fout:
        while True:
            for key, _ in sel.select():
                line = key.fileobj.readline()
                if not line:
                    return stdout, stderr
                if start_streaming_at in line:
                    start_streaming_flag = True
                if key.fileobj is out_io:
                    stdout += line
                    fout.write(line)
                    fout.flush()
                else:
                    stderr += line
                    fout.write(line)
                    fout.flush()
                if stream_logs and start_streaming_flag:
                    print(line, end='')
#!/usr/bin/env python

import sys
import os
import signal
from time import sleep
from tempfile import NamedTemporaryFile
from collections import namedtuple
import click
import yaml

import boto
from boto.ec2 import connect_to_region
import boto.vpc
import boto.iam
from boto.exception import EC2ResponseError

# Ansible configuration variables to set before importing Ansible modules
os.environ['ANSIBLE_SSH_ARGS'] = "-o ControlMaster=auto -o ControlPersist=60s -o UserKnownHostsFile=/dev/null"
os.environ['ANSIBLE_RECORD_HOST_KEYS'] = "False"
os.environ['ANSIBLE_HOST_KEY_CHECKING'] = "False"
os.environ['ANSIBLE_SSH_PIPELINING'] = "True"

from ansible.inventory import Inventory
from ansible.vars import VariableManager
from ansible.parsing.dataloader import DataLoader
from ansible.executor import playbook_executor
from ansible.utils.display import Display
from ansible.plugins.callback import CallbackBase

from myria.cluster.playbooks import playbooks_dir

from distutils.spawn import find_executable
import pkg_resources
VERSION = pkg_resources.get_distribution("myria-cluster").version
CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])

SCRIPT_NAME =  os.path.basename(sys.argv[0])
# this is necessary because pip loses executable permissions and ansible requires scripts to be executable
INVENTORY_SCRIPT_PATH = find_executable("ec2.py")
ANSIBLE_GLOBAL_VARS_PATH = os.path.join(playbooks_dir, "group_vars/all")
MAX_CONCURRENT_TASKS = 20 # more than this can trigger "too many open files" on Mac
MAX_RETRIES = 5

USER = os.getenv('USER')
HOME = os.getenv('HOME')

# valid log4j log levels (https://logging.apache.org/log4j/1.2/apidocs/org/apache/log4j/Level.html)
LOG_LEVELS = ['OFF', 'FATAL', 'ERROR', 'WARN', 'DEBUG', 'TRACE', 'ALL']

DEFAULTS = dict(
    key_pair="%s-myria" % USER,
    region='us-west-2',
    instance_type='t2.large',
    cluster_size=5,
    data_volume_size_gb=20,
    worker_mem_gb=4.0,
    worker_vcores=1,
    node_mem_gb=6.0,
    node_vcores=2,
    workers_per_node=1,
    cluster_log_level='WARN'
)

DEFAULT_AMI_IDS = {
    'us-east-1': "ami-fce3c696",
    'us-west-2': "ami-9abea4fb",
    'us-west-1': "ami-06116566",
    'eu-west-1': "ami-f95ef58a",
    'eu-central-1': "ami-87564feb",
    'ap-southeast-1': "ami-25c00c46",
    'ap-northeast-1': "ami-a21529cc",
    'ap-southeast-2': "ami-6c14310f",
    'ap-northeast-2': "ami-09dc1267",
    'sa-east-1': "ami-0fb83963"
}

ANSIBLE_GLOBAL_VARS = yaml.load(file(ANSIBLE_GLOBAL_VARS_PATH, 'r'))


class Options(object):
    """
    Options class to replace Ansible OptParser
    """
    def __init__(self, subset=None, syntax=False, listhosts=False, listtasks=False, listtags=False, module_path=None,
                 forks=MAX_CONCURRENT_TASKS, connection='smart', remote_user=None, private_key_file=None,
                 ssh_common_args=None, sftp_extra_args=None, scp_extra_args=None, ssh_extra_args=None,
                 become=False, become_method='sudo', become_user='root', verbosity=0, check=False):
        self.subset = subset
        self.syntax = syntax
        self.listhosts = listhosts
        self.listtasks = listtasks
        self.listtags = listtags
        self.module_path = module_path
        self.forks = forks
        self.connection = connection
        self.remote_user = remote_user
        self.private_key_file = private_key_file
        self.ssh_common_args = ssh_common_args
        self.sftp_extra_args = sftp_extra_args
        self.scp_extra_args = scp_extra_args
        self.ssh_extra_args = ssh_extra_args
        self.become = become
        self.become_method = become_method
        self.become_user = become_user
        self.verbosity = verbosity
        self.check = check


class Runner(object):

    def __init__(self, hostnames, playbook, private_key_file, run_data, become_pass=None,
                 verbosity=0, callback=None, subset_pattern=None):

        self.hostnames = hostnames

        self.playbook = os.path.join(playbooks_dir, playbook)
        self.run_data = run_data

        self.options = Options(subset=subset_pattern, private_key_file=private_key_file, verbosity=verbosity)

        self.display = Display()
        self.display.verbosity = verbosity
        playbook_executor.verbosity = verbosity

        passwords = {'become_pass': None}

        # Gets data from YAML/JSON files
        self.loader = DataLoader()
        self.loader.set_vault_password(os.environ.get('VAULT_PASS'))

        self.variable_manager = VariableManager()
        self.variable_manager.extra_vars = self.run_data

        self.inventory = Inventory(loader=self.loader, variable_manager=self.variable_manager, host_list=self.hostnames)
        self.variable_manager.set_inventory(self.inventory)

        self.pbex = playbook_executor.PlaybookExecutor(
            playbooks=[self.playbook],
            inventory=self.inventory,
            variable_manager=self.variable_manager,
            loader=self.loader,
            options=self.options,
            passwords=passwords)

        if callback:
            self.pbex._tqm._stdout_callback = callback

    def run(self):
        self.pbex.run()
        stats = self.pbex._tqm._stats

        run_success = True
        hosts = sorted(stats.processed.keys())
        for h in hosts:
            t = stats.summarize(h)
            if t['unreachable'] > 0 or t['failures'] > 0:
                run_success = False

        return run_success


class CallbackModule(CallbackBase):
    """
    Reference: https://github.com/ansible/ansible/blob/v2.0.0.2-1/lib/ansible/plugins/callback/default.py
    """

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = 'stored'
    CALLBACK_NAME = 'myria'

    def __init__(self, verbosity, retry_hosts):
        super(CallbackModule, self).__init__()
        self.verbosity = verbosity
        self.retry_hosts = retry_hosts

    def echo(self, msg):
        if self.verbosity > 0:
            click.echo(msg)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        delegated_vars = result._result.get('_ansible_delegated_vars', None)

        # Add the failed host to set of hosts to retry
        self.retry_hosts.add(result._host.get_name())

        # Catch an exception
        # This may never be called because default handler deletes
        # the exception, since Ansible thinks it knows better
        if 'exception' in result._result:
            # Extract the error message and log it
            # error = result._result['exception'].strip().split('\n')[-1]
            # print(error)
            msg = "An exception occurred during task execution. The full traceback is:\n" + result._result['exception']
            self.echo(msg)

            # Remove the exception from the result so it's not shown every time
            del result._result['exception']

        # Else log the reason for the failure
        if result._task.loop and 'results' in result._result:
            self._process_items(result)  # item_on_failed, item_on_skipped, item_on_ok
        else:
            if delegated_vars:
                self.echo("fatal: [%s -> %s]: FAILED! => %s" % (result._host.get_name(), delegated_vars['ansible_host'], self._dump_results(result._result)))
            else:
                self.echo("fatal: [%s]: FAILED! => %s" % (result._host.get_name(), self._dump_results(result._result)))

    def v2_runner_on_ok(self, result):
        self._clean_results(result._result, result._task.action)
        delegated_vars = result._result.get('_ansible_delegated_vars', None)
        if result._task.action == 'include':
            return
        elif result._result.get('changed', False):
            if delegated_vars:
                msg = "changed: [%s -> %s]" % (result._host.get_name(), delegated_vars['ansible_host'])
            else:
                msg = "changed: [%s]" % result._host.get_name()
        else:
            if delegated_vars:
                msg = "ok: [%s -> %s]" % (result._host.get_name(), delegated_vars['ansible_host'])
            else:
                msg = "ok: [%s]" % result._host.get_name()

        if result._task.loop and 'results' in result._result:
            self._process_items(result)  # item_on_failed, item_on_skipped, item_on_ok
        else:
            self.echo(msg)

    def v2_runner_on_skipped(self, result):
        if result._task.loop and 'results' in result._result:
            self._process_items(result)  # item_on_failed, item_on_skipped, item_on_ok
        else:
            msg = "skipping: [%s]" % result._host.get_name()
            self.echo(msg)

    def v2_runner_on_unreachable(self, result):
        # Add the failed host to set of hosts to retry
        self.retry_hosts.add(result._host.get_name())

        delegated_vars = result._result.get('_ansible_delegated_vars', None)
        if delegated_vars:
            self.echo("fatal: [%s -> %s]: UNREACHABLE! => %s" % (result._host.get_name(), delegated_vars['ansible_host'], self._dump_results(result._result)))
        else:
            self.echo("fatal: [%s]: UNREACHABLE! => %s" % (result._host.get_name(), self._dump_results(result._result)))

    def v2_runner_on_no_hosts(self, task):
        self.echo("skipping: no hosts matched")

    def v2_playbook_on_task_start(self, task, is_conditional):
        self.echo("TASK [%s]" % task.get_name().strip())

    def v2_playbook_on_play_start(self, play):
        name = play.get_name().strip()
        if not name:
            msg = "PLAY"
        else:
            msg = "PLAY [%s]" % name

        self.echo(msg)

    def v2_playbook_item_on_ok(self, result):
        delegated_vars = result._result.get('_ansible_delegated_vars', None)
        if result._task.action == 'include':
            return
        elif result._result.get('changed', False):
            if delegated_vars:
                msg = "changed: [%s -> %s]" % (result._host.get_name(), delegated_vars['ansible_host'])
            else:
                msg = "changed: [%s]" % result._host.get_name()
        else:
            if delegated_vars:
                msg = "ok: [%s -> %s]" % (result._host.get_name(), delegated_vars['ansible_host'])
            else:
                msg = "ok: [%s]" % result._host.get_name()

        msg += " => (item=%s)" % (result._result['item'])

        self.echo(msg)

    def v2_playbook_item_on_failed(self, result):
        # Add the failed host to set of hosts to retry
        self.retry_hosts.add(result._host.get_name())

        delegated_vars = result._result.get('_ansible_delegated_vars', None)
        if 'exception' in result._result:
            msg = "An exception occurred during task execution. The full traceback is:\n" + result._result['exception']
            self.echo(msg)
            # Remove the exception from the result so it's not shown every time
            del result._result['exception']

        if delegated_vars:
            self.echo("failed: [%s -> %s] => (item=%s) => %s" % (result._host.get_name(), delegated_vars['ansible_host'], result._result['item'], self._dump_results(result._result)))
        else:
            self.echo("failed: [%s] => (item=%s) => %s" % (result._host.get_name(), result._result['item'], self._dump_results(result._result)))

    def v2_playbook_item_on_skipped(self, result):
        msg = "skipping: [%s] => (item=%s) " % (result._host.get_name(), result._result['item'])
        self.echo(msg)

    def v2_playbook_on_stats(self, stats):
        hosts = sorted(stats.processed.keys())
        for h in hosts:
            t = stats.summarize(h)

            msg = "PLAY RECAP [%s] : %s %s %s %s %s" % (
                h,
                "ok: %s" % (t['ok']),
                "changed: %s" % (t['changed']),
                "unreachable: %s" % (t['unreachable']),
                "skipped: %s" % (t['skipped']),
                "failed: %s" % (t['failures']),
            )

            self.echo(msg)


def get_security_group_for_cluster(cluster_name, region, profile=None, vpc_id=None):
    ec2 = connect_to_region(region, profile_name=profile)
    groups = []
    if vpc_id:
        # In the EC2 API, filters can only express OR,
        # so we have to implement AND by intersecting results for each filter.
        groups_in_vpc = ec2.get_all_security_groups(filters={'vpc-id': vpc_id})
        groups = [g for g in groups_in_vpc if g.name == cluster_name]
    else:
        groups_with_name = ec2.get_all_security_groups(filters={'group-name': cluster_name})
        groups = groups_with_name
    if len(groups) == 0: # no groups found
        raise ValueError("No security groups found with name '%s'" % cluster_name)
    elif len(groups) > 1: # multiple groups found
        raise ValueError("Multiple security groups found with name '%s'" % cluster_name)
    return groups[0]


def terminate_cluster(cluster_name, region, profile=None, vpc_id=None):
    group = get_security_group_for_cluster(cluster_name, region, profile=profile, vpc_id=vpc_id)
    instance_ids = [instance.id for instance in group.instances()]
    # we want to allow users to delete a security group with no instances
    if instance_ids:
        click.echo("Terminating instances %s" % ', '.join(instance_ids))
        ec2 = connect_to_region(region, profile_name=profile)
        ec2.terminate_instances(instance_ids=instance_ids)
    click.echo("Deleting security group %s (%s)" % (group.name, group.id))
    # EC2 can take a while to update dependencies, so retry until we succeed
    while True:
        try:
            group.delete()
        except EC2ResponseError as e:
            if e.error_code == "DependencyViolation":
                click.echo("Security group state still converging, retrying in 5 seconds...")
                sleep(5)
            else:
                raise
        else:
            click.echo("Security group %s (%s) successfully deleted" % (group.name, group.id))
            break


def get_coordinator_public_hostname(cluster_name, region, profile=None, vpc_id=None):
    coordinator_hostname = None
    group = get_security_group_for_cluster(cluster_name, region, profile=profile, vpc_id=vpc_id)
    for instance in group.instances():
        if instance.tags.get('cluster-role') == "coordinator":
            coordinator_hostname = instance.public_dns_name
            break
    return coordinator_hostname


def default_key_file_from_key_pair(ctx, param, value):
    if value is None:
        qualified_key_pair = "%s_%s" % (ctx.params['key_pair'], ctx.params['region'])
        if ctx.params['profile']:
            qualified_key_pair = "%s_%s_%s" % (ctx.params['key_pair'], ctx.params['profile'], ctx.params['region'])
        return "%s/.ssh/%s.pem" % (HOME, qualified_key_pair)
    else:
        return value


def default_ami_id_from_region(ctx, param, value):
    return value if value is not None else DEFAULT_AMI_IDS[ctx.params['region']]


def validate_subnet_id(ctx, param, value):
    if value is not None:
        if ctx.params.get('zone') is not None:
            raise click.BadParameter("Cannot specify --zone if --subnet-id is specified")
        vpc_conn = boto.vpc.connect_to_region(ctx.params['region'], profile_name=ctx.params.get('profile'))
        try:
            subnet = vpc_conn.get_all_subnets(subnet_ids=[value])[0]
            ctx.params['vpc_id'] = subnet.vpc_id
        except:
            raise click.BadParameter("Invalid subnet ID '%s' for region '%s'" % (value, ctx.params['region']))
        return value


def validate_log_level(ctx, param, value):
    if value is not None:
        if value not in LOG_LEVELS:
            raise click.BadParameter("valid log levels are %s" % ', '.join(LOG_LEVELS))
        return value


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option(version=VERSION)
def run():
    pass


@run.command('create')
@click.argument('cluster_name')
@click.option('-v', '--verbose', count=True)
@click.option('--profile', default=None, is_eager=True,
    help="Boto profile used to launch your cluster")
@click.option('--region', show_default=True, default=DEFAULTS['region'], is_eager=True,
    help="AWS region to launch your cluster in")
@click.option('--zone', show_default=True, default=None,
    help="AWS availability zone to launch your cluster in")
@click.option('--key-pair', show_default=True, default=DEFAULTS['key_pair'],
    help="EC2 key pair used to launch your cluster")
@click.option('--private-key-file', callback=default_key_file_from_key_pair,
    help="Private key file for your EC2 key pair [default: %s]" % ("%s/.ssh/%s-myria_%s.pem" % (HOME, USER, DEFAULTS['region'])))
@click.option('--instance-type', show_default=True, default=DEFAULTS['instance_type'],
    help="EC2 instance type for your cluster")
@click.option('--cluster-size', show_default=True, default=DEFAULTS['cluster_size'],
    help="Number of EC2 instances in your cluster")
@click.option('--ami-id', callback=default_ami_id_from_region,
    help="ID of the AMI (Amazon Machine Image) used for your EC2 instances")
@click.option('--subnet-id', default=None, callback=validate_subnet_id,
    help="ID of the VPC subnet in which to launch your EC2 instances")
@click.option('--role', help="Name of an IAM role used to launch your EC2 instances")
@click.option('--spot-price', help="Price in dollars of the maximum bid for an EC2 spot instance request")
@click.option('--data-volume-size-gb', show_default=True, default=DEFAULTS['data_volume_size_gb'],
    help="Size of each instance's EBS data volume (used by Hadoop and PostgreSQL) in GB")
@click.option('--worker-mem-gb', show_default=True, default=DEFAULTS['worker_mem_gb'],
    help="Physical memory (in GB) reserved for each Myria worker")
@click.option('--worker-vcores', show_default=True, default=DEFAULTS['worker_vcores'],
    help="Number of virtual CPUs reserved for each Myria worker")
@click.option('--node-mem-gb', show_default=True, default=DEFAULTS['node_mem_gb'],
    help="Physical memory (in GB) on each EC2 instance available for Myria processes")
@click.option('--node-vcores', show_default=True, default=DEFAULTS['node_vcores'],
    help="Number of virtual CPUs on each EC2 instance available for Myria processes")
@click.option('--workers-per-node', show_default=True, default=DEFAULTS['workers_per_node'],
    help="Number of Myria workers per cluster node")
@click.option('--cluster-log-level', show_default=True, callback=validate_log_level, default=DEFAULTS['cluster_log_level'],
    help="One of %s, from lowest to highest level of detail" % ', '.join(LOG_LEVELS))
def create_cluster(cluster_name, **kwargs):
    ec2_ini_tmpfile = NamedTemporaryFile(delete=False)
    os.environ['EC2_INI_PATH'] = ec2_ini_tmpfile.name
    vpc_id = kwargs.get('vpc_id')

    # for displaying example commands
    options_str = "--region %s" % kwargs['region']
    if kwargs['profile']:
        options_str += " --profile %s" % kwargs['profile']
    if vpc_id:
        options_str += " --vpc-id %s" % vpc_id

    # abort if credentials are not available
    try:
        connect_to_region(kwargs['region'], profile_name=kwargs['profile'])
    except:
        click.echo("""
Unable to connect to the '{region}' EC2 region using the '{profile}' profile.
Please ensure that your AWS credentials are correctly configured:

http://boto3.readthedocs.io/en/latest/guide/configuration.html
""".format(region=kwargs['region'], profile=kwargs['profile'] if kwargs['profile'] else "default"))
        sys.exit(1)

    # abort if vpc_id is not supplied and no default VPC exists
    if not vpc_id:
        vpc_conn = boto.vpc.connect_to_region(kwargs['region'], profile_name=kwargs['profile'])
        default_vpcs = vpc_conn.get_all_vpcs(filters={'isDefault': "true"})
        if not default_vpcs:
            click.echo("""
No default VPC is configured for your AWS account in the '{region}' region.
Please ask your administrator to create a default VPC or specify a VPC using the `--vpc-id` option.
""".format(region=kwargs['region']))
            sys.exit(1)

    # abort if cluster already exists
    try:
        get_security_group_for_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=vpc_id)
    except:
        pass
    else:
        click.echo("""
Cluster '{cluster_name}' already exists in the '{region}' region. If you wish to create a new cluster with the same name, first run

{script_name} destroy {cluster_name} {options}
""".format(script_name=SCRIPT_NAME, cluster_name=cluster_name, region=kwargs['region'], options=options_str))
        sys.exit(1)

    # extract IAM user name for resource tagging
    iam_conn = boto.iam.connect_to_region(kwargs['region'], profile_name=kwargs['profile'])
    iam_user = None
    try:
        # TODO: once we move to boto3, we can get better info on callling principal from boto3.sts.get_caller_identity()
        iam_user = iam_conn.get_user()['get_user_response']['get_user_result']['user']['user_name']
    except:
        pass
    if not iam_user and kwargs['verbose'] > 0:
        click.echo("Warning: unable to find IAM user with credentials provided. IAM user tagging will be disabled.")

    # install keyboard interrupt handler to destroy partially-deployed cluster
    # TODO: signal handlers are inherited by each child process spawned by Ansible,
    # so messages are (harmlessly) duplicated for each process.
    def signal_handler(sig, frame):
        # uninstall handler to prevent multiple calls
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        click.echo("User interrupted deployment, destroying cluster...")
        try:
            terminate_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=vpc_id)
        except:
            pass # best-effort
        sys.exit(1)

    signal.signal(signal.SIGINT, signal_handler)

    extra_vars = dict((k.upper(), v) for k, v in kwargs.iteritems() if v is not None)
    extra_vars.update(CLUSTER_NAME=cluster_name)
    extra_vars.update(USER=USER)
    extra_vars.update(ansible_python_interpreter='/usr/bin/env python')
    extra_vars.update(EC2_INI_PATH=ec2_ini_tmpfile.name)
    if iam_user:
        extra_vars.update(IAM_USER=iam_user)

    if kwargs['verbose'] > 0:
        for k, v in extra_vars.iteritems():
            click.echo("%s: %s" % (k, v))

    # run local playbook to launch EC2 instances
    failed_hosts = set()
    playbook_args = dict(
        hostnames=['localhost'],
        playbook="local.yml",
        private_key_file=kwargs['private_key_file'],
        run_data=extra_vars,
        verbosity=kwargs['verbose'],
        callback=CallbackModule(kwargs['verbose'], failed_hosts))
    local_runner = Runner(**playbook_args)
    success = False
    try:
        success = local_runner.run()
    except Exception as e:
        if kwargs['verbose'] > 0:
            click.echo(e)
        click.echo("Unexpected error, destroying cluster...")
        terminate_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=vpc_id)
        sys.exit(1)
    if not success:
        # If the local playbook fails, the only failed host must be localhost.
        assert failed_hosts
        click.echo("Failed to initialize EC2 instances, destroying cluster...")
        try:
            terminate_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=vpc_id)
        except:
            pass # best-effort
        sys.exit(1)

    # run remote playbook to provision EC2 instances
    retries = 0
    retry_hosts_pattern = None
    # TODO: exponential backoff for unreachable hosts?
    while True:
        retry_hosts = set()
        playbook_args.update(hostnames=INVENTORY_SCRIPT_PATH, playbook="remote.yml", callback=CallbackModule(kwargs['verbose'], retry_hosts), subset_pattern=retry_hosts_pattern)
        remote_runner = Runner(**playbook_args)
        try:
            success = remote_runner.run()
        except Exception as e:
            if kwargs['verbose'] > 0:
                click.echo(e)
            click.echo("Unexpected error, destroying cluster...")
            terminate_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=vpc_id)
            sys.exit(1)
        if not success:
            assert retry_hosts
            if retries < MAX_RETRIES:
                retries += 1
                retry_hosts_pattern = ",".join(retry_hosts)
                click.echo("Retrying playbook run on hosts %s (%d of %d)" % (retry_hosts_pattern, retries, MAX_RETRIES))
            else:
                click.echo("Maximum retries (%d) exceeded, destroying cluster..." % MAX_RETRIES)
                terminate_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=vpc_id)
                sys.exit(1)
        else:
            break

    coordinator_public_hostname = None
    try:
        coordinator_public_hostname = get_coordinator_public_hostname(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=vpc_id)
    except:
        pass
    if not coordinator_public_hostname:
        click.echo("Couldn't resolve coordinator public DNS, exiting (not destroying cluster)")
        sys.exit(1)

    click.echo("""
Your new Myria cluster '{cluster_name}' has been launched on Amazon EC2 in the '{region}' region.

View the Myria worker IDs and public hostnames of all nodes in this cluster (the coordinator has worker ID 0):
{script_name} list {cluster_name} {options}

Stop this cluster:
{script_name} stop {cluster_name} {options}

Start this cluster after stopping it:
{script_name} start {cluster_name} {options}

Destroy this cluster:
{script_name} destroy {cluster_name} {options}

Log into the coordinator node:
ssh -i {private_key_file} {remote_user}@{coordinator_public_hostname}

myria-web interface:
http://{coordinator_public_hostname}:{myria_web_port}

MyriaX REST endpoint:
http://{coordinator_public_hostname}:{myria_rest_port}

Ganglia web interface:
http://{coordinator_public_hostname}:{ganglia_web_port}

Jupyter notebook interface:
http://{coordinator_public_hostname}:{jupyter_web_port}
""".format(coordinator_public_hostname=coordinator_public_hostname, myria_web_port=ANSIBLE_GLOBAL_VARS['myria_web_port'],
           myria_rest_port=ANSIBLE_GLOBAL_VARS['myria_rest_port'], ganglia_web_port=ANSIBLE_GLOBAL_VARS['ganglia_web_port'],
           jupyter_web_port=ANSIBLE_GLOBAL_VARS['jupyter_web_port'], private_key_file=kwargs['private_key_file'],
           remote_user=ANSIBLE_GLOBAL_VARS['remote_user'], script_name=SCRIPT_NAME, cluster_name=cluster_name,
           region=kwargs['region'], options=options_str))


@run.command('destroy')
@click.argument('cluster_name')
@click.option('--profile', default=None,
    help="Boto profile used to launch your cluster")
@click.option('--region', show_default=True, default=DEFAULTS['region'],
    help="AWS region to launch your cluster in")
@click.option('--vpc-id', default=None,
    help="ID of the VPC (Virtual Private Cloud) used for your EC2 instances")
def destroy_cluster(cluster_name, **kwargs):
    try:
        terminate_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=kwargs['vpc_id'])
    except ValueError as e:
        click.echo(e.message)
        sys.exit(1)


@run.command('stop')
@click.argument('cluster_name')
@click.option('--profile', default=None,
    help="Boto profile used to launch your cluster")
@click.option('--region', show_default=True, default=DEFAULTS['region'],
    help="AWS region to launch your cluster in")
@click.option('--vpc-id', default=None,
    help="ID of the VPC (Virtual Private Cloud) used for your EC2 instances")
def stop_cluster(cluster_name, **kwargs):
    group = get_security_group_for_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=kwargs['vpc_id'])
    instance_ids = [instance.id for instance in group.instances()]
    click.echo("Stopping instances %s" % ', '.join(instance_ids))
    ec2 = connect_to_region(kwargs['region'], profile_name=kwargs['profile'])
    ec2.stop_instances(instance_ids=instance_ids)
    while True:
        for instance in group.instances():
            instance.update(validate=True)
            if instance.state != "stopped":
                click.echo("Instance %s not stopped, retrying in 30 seconds..." % instance.id)
                sleep(30)
                break # break out of for loop
        else: # all instances were stopped, so break out of while loop
            break

    options_str = "--region %s" % kwargs['region']
    if kwargs['profile']:
        options_str += " --profile %s" % kwargs['profile']
    if kwargs['vpc_id']:
        options_str += " --vpc-id %s" % kwargs['vpc_id']
    print("""
Your Myria cluster '{cluster_name}' in the AWS '{region}' region has been successfully stopped.
You can start this cluster again by running `{script_name} start {cluster_name} {options}`.
""".format(script_name=SCRIPT_NAME, cluster_name=cluster_name, region=kwargs['region'], options=options_str))


@run.command('start')
@click.argument('cluster_name')
@click.option('--profile', default=None,
    help="Boto profile used to launch your cluster")
@click.option('--region', show_default=True, default=DEFAULTS['region'],
    help="AWS region to launch your cluster in")
@click.option('--vpc-id', default=None,
    help="ID of the VPC (Virtual Private Cloud) used for your EC2 instances")
def start_cluster(cluster_name, **kwargs):
    group = get_security_group_for_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=kwargs['vpc_id'])
    instance_ids = [instance.id for instance in group.instances()]
    click.echo("Starting instances %s" % ', '.join(instance_ids))
    ec2 = connect_to_region(kwargs['region'], profile_name=kwargs['profile'])
    ec2.start_instances(instance_ids=instance_ids)
    while True:
        for instance in group.instances():
            instance.update(validate=True)
            if instance.state != "running":
                click.echo("Instance %s not started, retrying in 30 seconds..." % instance.id)
                sleep(30)
                break # break out of for loop
        else: # all instances were started, so break out of while loop
            break

    options_str = "--region %s" % kwargs['region']
    if kwargs['profile']:
        options_str += " --profile %s" % kwargs['profile']
    if kwargs['vpc_id']:
        options_str += " --vpc-id %s" % kwargs['vpc_id']
    print("""
Your Myria cluster '{cluster_name}' in the AWS '{region}' region has been successfully restarted.
The public hostnames of all nodes in this cluster have changed.
You can view the new values by running

{script_name} list {cluster_name} {options}

(note that the new coordinator has worker ID 0).
""".format(script_name=SCRIPT_NAME, cluster_name=cluster_name, region=kwargs['region'], options=options_str))


@run.command('list')
@click.argument('cluster_name', required=False)
@click.option('--profile', default=None,
    help="Boto profile used to launch your cluster")
@click.option('--region', show_default=True, default=DEFAULTS['region'],
    help="AWS region to launch your cluster in")
@click.option('--vpc-id', default=None,
    help="ID of the VPC (Virtual Private Cloud) used for your EC2 instances")
def list_cluster(cluster_name, **kwargs):
    if cluster_name is not None:
        group = get_security_group_for_cluster(cluster_name, kwargs['region'], profile=kwargs['profile'], vpc_id=kwargs['vpc_id'])
        format_str = "{: <10} {: <50}"
        print(format_str.format('WORKER_IDS', 'HOST'))
        print(format_str.format('----------', '----'))
        instances = sorted(group.instances(), key=lambda i: int(i.tags.get('worker-id').split(',')[0]))
        for instance in instances:
            print(format_str.format(instance.tags.get('worker-id'), instance.public_dns_name))
    else:
        ec2 = connect_to_region(kwargs['region'], profile_name=kwargs['profile'])
        myria_groups = ec2.get_all_security_groups(filters={'tag:app': "myria"})
        groups = myria_groups
        if kwargs['vpc_id']:
            groups_in_vpc = ec2.get_all_security_groups(filters={'vpc-id': kwargs['vpc_id']})
            groups_in_vpc_ids = [g.id for g in groups_in_vpc]
            # In the EC2 API, filters can only express OR,
            # so we have to implement AND by intersecting results for each filter.
            groups = [g for g in myria_groups if g.id in groups_in_vpc_ids]
        format_str = "{: <20} {: <5} {: <50}"
        print(format_str.format('CLUSTER', 'NODES', 'COORDINATOR'))
        print(format_str.format('-------', '-----', '-----------'))
        for group in groups:
            coordinator = ""
            for instance in group.instances():
                if instance.tags.get('cluster-role') == "coordinator":
                    coordinator = instance.public_dns_name
                    break
            print(format_str.format(group.name, len(group.instances()), coordinator))


if __name__ == '__main__':
    run()

# Copyright 2021 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
# with the License. A copy of the License is located at
#
# http://aws.amazon.com/apache2.0/
#
# or in the "LICENSE.txt" file accompanying this file. This file is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions and
# limitations under the License.
import difflib
import json
import os
import re
from abc import ABC, abstractmethod
from datetime import datetime

import pytest
import yaml
from assertpy import assert_that
from freezegun import freeze_time

from pcluster.aws.aws_resources import InstanceTypeInfo
from pcluster.constants import (
    MAX_EBS_COUNT,
    MAX_EXISTING_STORAGE_COUNT,
    MAX_NEW_STORAGE_COUNT,
    MAX_NUMBER_OF_COMPUTE_RESOURCES_PER_CLUSTER,
    MAX_NUMBER_OF_QUEUES,
)
from pcluster.models.s3_bucket import S3FileFormat, format_content
from pcluster.schemas.cluster_schema import ClusterSchema
from pcluster.templates.cdk_builder import CDKTemplateBuilder
from pcluster.utils import load_json_dict, load_yaml_dict
from tests.pcluster.aws.dummy_aws_api import mock_aws_api
from tests.pcluster.models.dummy_s3_bucket import dummy_cluster_bucket, mock_bucket, mock_bucket_object_utils
from tests.pcluster.utils import (
    assert_lambdas_have_expected_vpc_config_and_managed_policy,
    get_asset_content_with_resource_name,
    load_cluster_model_from_yaml,
)

EXAMPLE_CONFIGS_DIR = f"{os.path.abspath(os.path.join(__file__, '..', '..'))}/example_configs"
MAX_SIZE_OF_CFN_TEMPLATE = 1024 * 1024
MAX_RESOURCES_PER_TEMPLATE = 500


@pytest.mark.parametrize(
    "config_file_name",
    [
        "slurm.required.yaml",
        "slurm.full.yaml",
        "awsbatch.simple.yaml",
        "awsbatch.full.yaml",
        "scheduler_plugin.required.yaml",
        "scheduler_plugin.full.yaml",
    ],
)
def test_cluster_builder_from_configuration_file(
    mocker, capsys, pcluster_config_reader, test_datadir, config_file_name
):
    """Build CFN template starting from config examples."""
    mock_aws_api(mocker)
    # mock bucket initialization parameters
    mock_bucket(mocker)
    mock_bucket_object_utils(mocker)

    # Search config file from example_configs folder to test standard configuration
    _, cluster = load_cluster_model_from_yaml(config_file_name)
    _generate_template(cluster, capsys)


def _assert_config_snapshot(config, expected_full_config_path):
    """
    Confirm that no new configuration sections were added / removed.

    If any sections were added/removed:
    1. Add the section to the "slurm.full.all_resources.yaml" file
    2. Generate a new snapshot using the test output
    TODO: Use a snapshot testing library
    """
    cluster_name = "test_cluster"
    full_config = ClusterSchema(cluster_name).dump(config)
    full_config_yaml = yaml.dump(full_config)

    with open(expected_full_config_path, "r") as expected_full_config_file:
        expected_full_config = expected_full_config_file.read()
        diff = difflib.unified_diff(
            full_config_yaml.splitlines(keepends=True), expected_full_config.splitlines(keepends=True)
        )
        print("Diff between existing snapshot and new snapshot:")
        print("".join(diff), end="")
        assert_that(expected_full_config).is_equal_to(full_config_yaml)


def test_cluster_config_limits(mocker, capsys, tmpdir, pcluster_config_reader, test_datadir):
    """
    Build CFN template starting from config examples and assert CFN limits (file size and number of resources).

    In the config file we have defined all the possible resources, capped at the current validators limits.
    https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/cloudformation-limits.html
    """
    mock_aws_api(mocker)
    # mock bucket initialization parameters
    mock_bucket(mocker)
    mock_bucket_object_utils(mocker)

    # The max number of queues cannot be used with the max number of compute resources
    # (it will exceed the max number of compute resources per cluster)
    # This workaround uses half of the max number of queues. It then calculates the number of compute resources to use
    # as the quotient of dividing the max number of compute resources per cluster by the half.
    max_number_of_queues = MAX_NUMBER_OF_QUEUES // 2
    max_number_of_crs = MAX_NUMBER_OF_COMPUTE_RESOURCES_PER_CLUSTER // max_number_of_queues

    # Try to search for jinja templates in the test_datadir, this is mainly to verify pcluster limits
    rendered_config_file = pcluster_config_reader(
        "slurm.full.all_resources.yaml",
        max_ebs_count=MAX_EBS_COUNT,
        max_new_storage_count=MAX_NEW_STORAGE_COUNT,
        max_existing_storage_count=MAX_EXISTING_STORAGE_COUNT,
        # number of queues, compute resources and security groups highly impacts the size of AWS resources
        max_number_of_queues=max_number_of_queues,
        max_number_of_ondemand_crs=max_number_of_crs,
        max_number_of_spot_crs=max_number_of_crs,
        number_of_sg_per_queue=1,
        # The number of following items doesn't impact number of resources, but the size of the template.
        # We have to reduce number of tags, script args and remove dev settings to reduce template size,
        # because we're overcoming generated template size limits.
        number_of_tags=1,  # max number of tags is 50
        number_of_script_args=1,  # this is potentially unlimited
        dev_settings_enabled=False,  # these shouldn't be used by most of the users
    )
    input_yaml, cluster = load_cluster_model_from_yaml(rendered_config_file, test_datadir)

    # Confirm that the configuration file is not missing sections that would impact the size of the templates
    expected_full_config_path = test_datadir / "slurm.full_config.snapshot.yaml"
    _assert_config_snapshot(cluster, expected_full_config_path)

    # Generate CFN template file
    cluster_template, assets = _generate_template(cluster, capsys)
    cluster_template_as_yaml = format_content(cluster_template, S3FileFormat.YAML)  # Main template is YAML formatted
    assets_as_json = [
        format_content(asset, S3FileFormat.MINIFIED_JSON)  # Nested templates/assets as JSON Minified
        for asset in assets
    ]

    for template in [cluster_template_as_yaml] + assets_as_json:
        output_path = str(tmpdir / "generated_cfn_template")
        with open(output_path, "w") as output_file:
            output_file.write(template)
        _assert_template_limits(output_path, template)


def _assert_template_limits(template_path: str, template_content: str):
    """
    Assert that size of the template doesn't exceed 1MB and number of resources doesn't exceed 500.

    :param template_path: path to the generated cfn template
    """
    assert_that(os.stat(template_path).st_size).is_less_than(MAX_SIZE_OF_CFN_TEMPLATE)
    matches = len(re.findall("Type.*AWS::", str()))
    assert_that(matches).is_less_than(MAX_RESOURCES_PER_TEMPLATE)


def _generate_template(cluster, capsys):
    # Try to build the template
    generated_template, assets_metadata = CDKTemplateBuilder().build_cluster_template(
        cluster_config=cluster, bucket=dummy_cluster_bucket(), stack_name="clustername"
    )
    cluster_assets = [asset["content"] for asset in assets_metadata]
    _, err = capsys.readouterr()
    assert_that(err).is_empty()  # Assertion failure may become an update of dependency warning deprecations.
    return generated_template, cluster_assets


@pytest.mark.parametrize(
    "config_file_name",
    [
        "slurm.required.yaml",
        "slurm.full.yaml",
        "awsbatch.simple.yaml",
        "awsbatch.full.yaml",
        "scheduler_plugin.required.yaml",
        "scheduler_plugin.full.yaml",
    ],
)
def test_add_alarms(mocker, config_file_name):
    mock_aws_api(mocker)
    # mock bucket initialization parameters
    mock_bucket(mocker)
    mock_bucket_object_utils(mocker)

    input_yaml, cluster = load_cluster_model_from_yaml(config_file_name)
    generated_template, _ = CDKTemplateBuilder().build_cluster_template(
        cluster_config=cluster, bucket=dummy_cluster_bucket(), stack_name="clustername"
    )
    output_yaml = yaml.dump(generated_template, width=float("inf"))

    if cluster.is_cw_dashboard_enabled:
        assert_that(output_yaml).contains("HeadNodeDiskAlarm")
        assert_that(output_yaml).contains("HeadNodeMemAlarm")
    else:
        assert_that(output_yaml).does_not_contain("HeadNodeDiskAlarm")
        assert_that(output_yaml).does_not_contain("HeadNodeMemAlarm")


@pytest.mark.parametrize(
    "config_file_name, expected_scheduler_plugin_stack",
    [
        ("scheduler-plugin-without-template.yaml", {}),
        (
            "scheduler-plugin-with-template.yaml",
            {
                "Type": "AWS::CloudFormation::Stack",
                "Properties": {
                    "TemplateURL": "https://parallelcluster-a69601b5ee1fc2f2-v1-do-not-delete.s3.fake-region.amazonaws."
                    "com/parallelcluster/clusters/dummy-cluster-randomstring123/templates/"
                    "scheduler-plugin-substack.cfn",
                    "Parameters": {
                        "ClusterName": "clustername",
                        "ParallelClusterStackId": {"Ref": "AWS::StackId"},
                        "VpcId": "vpc-123",
                        "HeadNodeRoleName": {"Ref": "RoleHeadNode"},
                        "ComputeFleetRoleNames": {
                            "Fn::GetAtt": [
                                "ComputeFleetQueueBatch0QueueGroup0NestedStackQueueGroup0NestedStackResource356F7DC3",
                                "Outputs.clusternameComputeFleetQueueBatch0QueueGroup0Role15b342af42246b70E9AB1575Ref",
                            ]
                        },
                        "LaunchTemplate1f8c19f38f8d4f7fVersion": {
                            "Fn::GetAtt": [
                                "ComputeFleetQueueBatch0QueueGroup0NestedStackQueueGroup0NestedStackResource356F7DC3",
                                "Outputs.clusternameComputeFleetQueueBatch0QueueGroup0LaunchTemplate1f8c19f38f8d4f7f658"
                                + "C4380LatestVersionNumber",
                            ]
                        },
                        "LaunchTemplateA6f65dee6703df4aVersion": {
                            "Fn::GetAtt": [
                                "ComputeFleetQueueBatch0QueueGroup0NestedStackQueueGroup0NestedStackResource356F7DC3",
                                "Outputs.clusternameComputeFleetQueueBatch0QueueGroup0LaunchTemplateA6f65dee6703df4a05B"
                                + "14750LatestVersionNumber",
                            ]
                        },
                    },
                },
            },
        ),
        (
            "scheduler-plugin-with-head-node-instance-role.yaml",
            {
                "Type": "AWS::CloudFormation::Stack",
                "Properties": {
                    "TemplateURL": "https://parallelcluster-a69601b5ee1fc2f2-v1-do-not-delete.s3.fake-region."
                    "amazonaws.com/parallelcluster/clusters/dummy-cluster-randomstring123/templates/"
                    "scheduler-plugin-substack.cfn",
                    "Parameters": {
                        "ClusterName": "clustername",
                        "ParallelClusterStackId": {"Ref": "AWS::StackId"},
                        "VpcId": "vpc-123",
                        "HeadNodeRoleName": "",
                        "ComputeFleetRoleNames": {
                            "Fn::GetAtt": [
                                "ComputeFleetQueueBatch0QueueGroup0NestedStackQueueGroup0NestedStackResource356F7DC3",
                                "Outputs.clusternameComputeFleetQueueBatch0QueueGroup0Role15b342af42246b70E9AB1575Ref",
                            ]
                        },
                        "LaunchTemplate1f8c19f38f8d4f7fVersion": {
                            "Fn::GetAtt": [
                                "ComputeFleetQueueBatch0QueueGroup0NestedStackQueueGroup0NestedStackResource356F7DC3",
                                "Outputs.clusternameComputeFleetQueueBatch0QueueGroup0LaunchTemplate1f8c19f38f8d4f7f658"
                                + "C4380LatestVersionNumber",
                            ]
                        },
                        "LaunchTemplateA6f65dee6703df4aVersion": {
                            "Fn::GetAtt": [
                                "ComputeFleetQueueBatch0QueueGroup0NestedStackQueueGroup0NestedStackResource356F7DC3",
                                "Outputs.clusternameComputeFleetQueueBatch0QueueGroup0LaunchTemplateA6f65dee6703df4a05B"
                                + "14750LatestVersionNumber",
                            ]
                        },
                    },
                },
            },
        ),
        (
            "scheduler-plugin-with-compute-fleet-instance-role.yaml",
            {
                "Type": "AWS::CloudFormation::Stack",
                "Properties": {
                    "TemplateURL": "https://parallelcluster-a69601b5ee1fc2f2-v1-do-not-delete.s3.fake-region.amazonaws."
                    "com/parallelcluster/clusters/dummy-cluster-randomstring123/templates/"
                    "scheduler-plugin-substack.cfn",
                    "Parameters": {
                        "ClusterName": "clustername",
                        "ParallelClusterStackId": {"Ref": "AWS::StackId"},
                        "VpcId": "vpc-123",
                        "HeadNodeRoleName": "",
                        "ComputeFleetRoleNames": "",
                        "LaunchTemplate1f8c19f38f8d4f7fVersion": {
                            "Fn::GetAtt": [
                                "ComputeFleetQueueBatch0QueueGroup0NestedStackQueueGroup0NestedStackResource356F7DC3",
                                "Outputs.clusternameComputeFleetQueueBatch0QueueGroup0LaunchTemplate1f8c19f38f8d4f7f658"
                                + "C4380LatestVersionNumber",
                            ]
                        },
                        "LaunchTemplate7916067054f91933Version": {
                            "Fn::GetAtt": [
                                "ComputeFleetQueueBatch0QueueGroup0NestedStackQueueGroup0NestedStackResource356F7DC3",
                                "Outputs.clusternameComputeFleetQueueBatch0QueueGroup0LaunchTemplate7916067054f919335AF"
                                + "28643LatestVersionNumber",
                            ]
                        },
                        "LaunchTemplateA6f65dee6703df4aVersion": {
                            "Fn::GetAtt": [
                                "ComputeFleetQueueBatch0QueueGroup0NestedStackQueueGroup0NestedStackResource356F7DC3",
                                "Outputs.clusternameComputeFleetQueueBatch0QueueGroup0LaunchTemplateA6f65dee6703df4a05B"
                                + "14750LatestVersionNumber",
                            ]
                        },
                    },
                },
            },
        ),
        (
            "scheduler_plugin.full.yaml",
            {
                "Type": "AWS::CloudFormation::Stack",
                "Properties": {
                    "TemplateURL": "https://parallelcluster-a69601b5ee1fc2f2-v1-do-not-delete.s3.fake-region.amazonaws."
                    "com/parallelcluster/clusters/dummy-cluster-randomstring123/templates/"
                    "scheduler-plugin-substack.cfn",
                    "Parameters": {
                        "ClusterName": "clustername",
                        "ParallelClusterStackId": {"Ref": "AWS::StackId"},
                        "VpcId": "vpc-123",
                        "HeadNodeRoleName": "",
                        "ComputeFleetRoleNames": {
                            "Fn::GetAtt": [
                                "ComputeFleetQueueBatch0QueueGroup0NestedStackQueueGroup0NestedStackResource356F7DC3",
                                "Outputs.clusternameComputeFleetQueueBatch0QueueGroup0Role15b342af42246b70E9AB1575Ref",
                            ]
                        },
                        "LaunchTemplate1f8c19f38f8d4f7fVersion": {
                            "Fn::GetAtt": [
                                "ComputeFleetQueueBatch0QueueGroup0NestedStackQueueGroup0NestedStackResource356F7DC3",
                                "Outputs.clusternameComputeFleetQueueBatch0QueueGroup0LaunchTemplate1f8c19f38f8d4f7f658"
                                + "C4380LatestVersionNumber",
                            ]
                        },
                        "LaunchTemplate7916067054f91933Version": {
                            "Fn::GetAtt": [
                                "ComputeFleetQueueBatch0QueueGroup0NestedStackQueueGroup0NestedStackResource356F7DC3",
                                "Outputs.clusternameComputeFleetQueueBatch0QueueGroup0LaunchTemplate7916067054f919335AF"
                                + "28643LatestVersionNumber",
                            ]
                        },
                        "LaunchTemplateA46d18b906a50d3aVersion": {
                            "Fn::GetAtt": [
                                "ComputeFleetQueueBatch0QueueGroup0NestedStackQueueGroup0NestedStackResource356F7DC3",
                                "Outputs.clusternameComputeFleetQueueBatch0QueueGroup0LaunchTemplateA46d18b906a50d3a3A2"
                                + "D0E8FLatestVersionNumber",
                            ]
                        },
                        "LaunchTemplateA6f65dee6703df4aVersion": {
                            "Fn::GetAtt": [
                                "ComputeFleetQueueBatch0QueueGroup0NestedStackQueueGroup0NestedStackResource356F7DC3",
                                "Outputs.clusternameComputeFleetQueueBatch0QueueGroup0LaunchTemplateA6f65dee6703df4a05B"
                                + "14750LatestVersionNumber",
                            ]
                        },
                    },
                },
            },
        ),
    ],
)
def test_scheduler_plugin_substack(mocker, config_file_name, expected_scheduler_plugin_stack, test_datadir):
    mock_aws_api(mocker)
    # mock bucket initialization parameters
    mock_bucket(mocker)
    mock_bucket_object_utils(mocker)

    if config_file_name == "scheduler_plugin.full.yaml":
        input_yaml, cluster = load_cluster_model_from_yaml(config_file_name)
    else:
        input_yaml, cluster = load_cluster_model_from_yaml(config_file_name, test_datadir)
    generated_template, _ = CDKTemplateBuilder().build_cluster_template(
        cluster_config=cluster, bucket=dummy_cluster_bucket(), stack_name="clustername"
    )
    print(yaml.dump(generated_template))
    assert_that(generated_template["Resources"].get("SchedulerPluginStack", {})).is_equal_to(
        expected_scheduler_plugin_stack
    )


def _mock_instance_type_info(instance_type):
    instance_types_info = {
        "c4.xlarge": InstanceTypeInfo(
            {
                "InstanceType": "c4.xlarge",
                "VCpuInfo": {
                    "DefaultVCpus": 4,
                    "DefaultCores": 2,
                    "DefaultThreadsPerCore": 2,
                    "ValidCores": [1, 2],
                    "ValidThreadsPerCore": [1, 2],
                },
                "EbsInfo": {"EbsOptimizedSupport": "default"},
                "NetworkInfo": {"EfaSupported": False, "MaximumNetworkCards": 3},
                "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]},
            }
        ),
        "t2.micro": InstanceTypeInfo(
            {
                "InstanceType": "t2.micro",
                "VCpuInfo": {
                    "DefaultVCpus": 4,
                    "DefaultCores": 2,
                    "DefaultThreadsPerCore": 2,
                    "ValidCores": [1, 2],
                    "ValidThreadsPerCore": [1, 2],
                },
                "EbsInfo": {"EbsOptimizedSupport": "unsupported"},
                "NetworkInfo": {"EfaSupported": False, "MaximumNetworkCards": 2},
                "ProcessorInfo": {"SupportedArchitectures": ["x86_64"]},
            }
        ),
    }

    return instance_types_info[instance_type]


def get_launch_template_data_property(lt_property, template, lt_name):
    return (
        template["Resources"]
        .get(lt_name, {})
        .get("Properties", {})
        .get("LaunchTemplateData", {})
        .get(lt_property, None)
    )


class LTPropertyAssertion(ABC):
    def __init__(self, **assertion_params):
        self.assertion_params = assertion_params

    @abstractmethod
    def assert_lt_properties(self, generated_template, lt_name):
        pass


class NetworkInterfaceLTAssertion(LTPropertyAssertion):
    def assert_lt_properties(self, generated_template, lt_name):
        network_interfaces = get_launch_template_data_property("NetworkInterfaces", generated_template, lt_name)
        assert_that(network_interfaces).is_length(self.assertion_params.get("no_of_network_interfaces"))
        for network_interface in network_interfaces:
            assert_that(network_interface.get("SubnetId")).is_equal_to(self.assertion_params.get("subnet_id"))


class InstanceTypeLTAssertion(LTPropertyAssertion):
    def assert_lt_properties(self, generated_template, lt_name):
        instance_type = get_launch_template_data_property("InstanceType", generated_template, lt_name)
        if self.assertion_params.get("has_instance_type"):
            assert_that(instance_type).is_not_none()
        else:
            assert_that(instance_type).is_none()


class EbsLTAssertion(LTPropertyAssertion):
    def assert_lt_properties(self, generated_template, lt_name):
        ebs_optimized = get_launch_template_data_property("EbsOptimized", generated_template, lt_name)
        if self.assertion_params.get("includes_ebs_optimized"):
            assert_that(ebs_optimized).is_equal_to(self.assertion_params.get("is_ebs_optimized"))
        else:
            assert_that(ebs_optimized).is_none()


@pytest.mark.parametrize(
    "config_file_name, lt_assertions",
    [
        (
            "cluster-using-flexible-instance-types.yaml",
            [
                NetworkInterfaceLTAssertion(no_of_network_interfaces=2, subnet_id=None),
                InstanceTypeLTAssertion(has_instance_type=False),
                EbsLTAssertion(includes_ebs_optimized=False, is_ebs_optimized=None),
            ],
        ),
        (
            "cluster-using-single-instance-type.yaml",
            [
                NetworkInterfaceLTAssertion(no_of_network_interfaces=3, subnet_id="subnet-12345678"),
                InstanceTypeLTAssertion(has_instance_type=True),
                EbsLTAssertion(includes_ebs_optimized=True, is_ebs_optimized=True),
            ],
        ),
    ],
)
def test_compute_launch_template_properties(
    mocker,
    config_file_name,
    lt_assertions,
    test_datadir,
):
    mock_aws_api(mocker, mock_instance_type_info=False)

    mocker.patch(
        "pcluster.aws.ec2.Ec2Client.get_instance_type_info",
        side_effect=_mock_instance_type_info,
    )
    # mock bucket initialization parameters
    mock_bucket(mocker)
    mock_bucket_object_utils(mocker)

    input_yaml, cluster = load_cluster_model_from_yaml(config_file_name, test_datadir)
    generated_template, cdk_assets = CDKTemplateBuilder().build_cluster_template(
        cluster_config=cluster, bucket=dummy_cluster_bucket(), stack_name="clustername"
    )

    asset_content = get_asset_content_with_resource_name(cdk_assets, "LaunchTemplate64e1c3597ca4c326")

    for lt_assertion in lt_assertions:
        lt_assertion.assert_lt_properties(asset_content, "LaunchTemplate64e1c3597ca4c326")


@pytest.mark.parametrize(
    "config_file_name, expected_head_node_dna_json_fields",
    [
        ("slurm-imds-secured-true.yaml", {"scheduler": "slurm", "head_node_imds_secured": "true"}),
        (
            "slurm-imds-secured-false.yaml",
            {"scheduler": "slurm", "head_node_imds_secured": "false", "compute_node_bootstrap_timeout": 1000},
        ),
        (
            "awsbatch-imds-secured-false.yaml",
            {"scheduler": "awsbatch", "head_node_imds_secured": "false", "compute_node_bootstrap_timeout": 1201},
        ),
        ("scheduler-plugin-imds-secured-true.yaml", {"scheduler": "plugin", "head_node_imds_secured": "true"}),
        (
            "scheduler-plugin-headnode-hooks-partial.yaml",
            {
                "scheduler": "plugin",
            },
        ),
        (
            "awsbatch-headnode-hooks-partial.yaml",
            {
                "scheduler": "awsbatch",
            },
        ),
        (
            "slurm-headnode-hooks-full.yaml",
            {
                "scheduler": "slurm",
            },
        ),
    ],
)
# Datetime mocking is required because some template values depend on the current datetime value
@freeze_time("2021-01-01T01:01:01")
def test_head_node_dna_json(mocker, test_datadir, config_file_name, expected_head_node_dna_json_fields):
    default_head_node_dna_json = load_json_dict(test_datadir / "head_node_default.dna.json")

    mock_aws_api(mocker)
    mock_bucket_object_utils(mocker)

    input_yaml = load_yaml_dict(test_datadir / config_file_name)

    cluster_config = ClusterSchema(cluster_name="clustername").load(input_yaml)

    generated_template, _ = CDKTemplateBuilder().build_cluster_template(
        cluster_config=cluster_config, bucket=dummy_cluster_bucket(), stack_name="clustername"
    )

    generated_head_node_dna_json = json.loads(
        _get_cfn_init_file_content(template=generated_template, resource="HeadNodeLaunchTemplate", file="/tmp/dna.json")
    )
    plugin__specific_settings = {
        "scheduler_plugin_substack_arn": "{'Ref': 'SchedulerPluginStack'}",
        "ddb_table": "{'Ref': 'DynamoDBTable'}",
    }
    slurm_specific_settings = {
        "ddb_table": "{'Ref': 'DynamoDBTable'}",
        "dns_domain": "{'Ref': 'ClusterDNSDomain'}",
        "hosted_zone": "{'Ref': 'Route53HostedZone'}",
        "slurm_ddb_table": "{'Ref': 'SlurmDynamoDBTable'}",
        "use_private_hostname": "false",
    }

    if expected_head_node_dna_json_fields["scheduler"] == "slurm":
        default_head_node_dna_json["cluster"].update(slurm_specific_settings)
    elif expected_head_node_dna_json_fields["scheduler"] == "plugin":
        default_head_node_dna_json["cluster"].update(plugin__specific_settings)

    default_head_node_dna_json["cluster"].update(expected_head_node_dna_json_fields)

    assert_that(generated_head_node_dna_json).is_equal_to(default_head_node_dna_json)


@freeze_time("2021-01-01T01:01:01")
@pytest.mark.parametrize(
    "config_file_name, expected_head_node_bootstrap_timeout",
    [
        ("slurm.required.yaml", "1800"),
        ("slurm.full.yaml", "1201"),
        ("awsbatch.simple.yaml", "1800"),
        ("awsbatch.full.yaml", "1000"),
        ("scheduler_plugin.required.yaml", "1800"),
        ("scheduler_plugin.full.yaml", "1201"),
    ],
)
def test_head_node_bootstrap_timeout(mocker, config_file_name, expected_head_node_bootstrap_timeout):
    mock_aws_api(mocker)
    # mock bucket initialization parameters
    mock_bucket(mocker)
    mock_bucket_object_utils(mocker)

    input_yaml, cluster = load_cluster_model_from_yaml(config_file_name)
    generated_template, _ = CDKTemplateBuilder().build_cluster_template(
        cluster_config=cluster, bucket=dummy_cluster_bucket(), stack_name="clustername"
    )
    assert_that(
        generated_template["Resources"]
        .get("HeadNodeWaitCondition" + datetime.utcnow().strftime("%Y%m%d%H%M%S"))
        .get("Properties")
        .get("Timeout")
    ).is_equal_to(expected_head_node_bootstrap_timeout)


def _get_cfn_init_file_content(template, resource, file):
    cfn_init = template["Resources"][resource]["Metadata"]["AWS::CloudFormation::Init"]
    content_join = cfn_init["deployConfigFiles"]["files"][file]["content"]["Fn::Join"]
    content_separator = content_join[0]
    content_elements = content_join[1]
    return content_separator.join(str(elem) for elem in content_elements)


@freeze_time("2021-01-01T01:01:01")
@pytest.mark.parametrize(
    "config_file_name, expected_instance_tags, expected_volume_tags",
    [
        (
            "slurm.full.yaml",
            {},
            {
                "parallelcluster:cluster-name": "clustername",
                "parallelcluster:node-type": "HeadNode",
                # TODO The tag 'parallelcluster:version' is actually included within head node volume tags,
                #  but some refactoring is required to check it within this test.
                # "parallelcluster:version": "[0-9\\.A-Za-z]+",
                "String": "String",
                "two": "two22",
            },
        ),
        (
            "awsbatch.full.yaml",
            {},
            {
                "parallelcluster:cluster-name": "clustername",
                "parallelcluster:node-type": "HeadNode",
                # TODO The tag 'parallelcluster:version' is actually included within head node volume tags,
                #  but some refactoring is required to check it within this test.
                # "parallelcluster:version": "[0-9\\.A-Za-z]+",
                "String": "String",
                "two": "two22",
            },
        ),
        (
            "scheduler_plugin.full.yaml",
            {},
            {
                "parallelcluster:cluster-name": "clustername",
                "parallelcluster:node-type": "HeadNode",
                # TODO The tag 'parallelcluster:version' is actually included within head node volume tags,
                #  but some refactoring is required to check it within this test.
                # "parallelcluster:version": "[0-9\\.A-Za-z]+",
                "String": "String",
                "two": "two22",
            },
        ),
    ],
)
def test_head_node_tags_from_launch_template(mocker, config_file_name, expected_instance_tags, expected_volume_tags):
    mock_aws_api(mocker)
    mock_bucket(mocker)
    mock_bucket_object_utils(mocker)
    input_yaml, cluster = load_cluster_model_from_yaml(config_file_name)
    generated_template, _ = CDKTemplateBuilder().build_cluster_template(
        cluster_config=cluster, bucket=dummy_cluster_bucket(), stack_name="clustername"
    )
    tags_specifications = (
        generated_template.get("Resources")
        .get("HeadNodeLaunchTemplate")
        .get("Properties")
        .get("LaunchTemplateData")
        .get("TagSpecifications", [])
    )

    instance_tags = next((specs for specs in tags_specifications if specs["ResourceType"] == "instance"), {}).get(
        "Tags", []
    )
    actual_instance_tags = {tag["Key"]: tag["Value"] for tag in instance_tags}
    assert_that(actual_instance_tags).is_equal_to(expected_instance_tags)

    volume_tags = next((specs for specs in tags_specifications if specs["ResourceType"] == "volume"), {}).get(
        "Tags", []
    )
    actual_volume_tags = {tag["Key"]: tag["Value"] for tag in volume_tags}
    assert_that(actual_volume_tags).is_equal_to(expected_volume_tags)


@freeze_time("2021-01-01T01:01:01")
@pytest.mark.parametrize(
    "config_file_name, expected_tags",
    [
        (
            "slurm.full.yaml",
            {
                "Name": "HeadNode",
                "parallelcluster:cluster-name": "clustername",
                "parallelcluster:node-type": "HeadNode",
                "parallelcluster:attributes": "centos7, slurm, [0-9\\.A-Za-z]+, x86_64",
                "parallelcluster:filesystem": "efs=1, multiebs=1, raid=0, fsx=3",
                "parallelcluster:networking": "EFA=NONE",
                # TODO The tag 'parallelcluster:version' is actually included within head node volume tags,
                #  but some refactoring is required to check it within this test.
                # "parallelcluster:version": "[0-9\\.A-Za-z]+",
                "String": "String",
                "two": "two22",
            },
        ),
        (
            "awsbatch.full.yaml",
            {
                "Name": "HeadNode",
                "parallelcluster:cluster-name": "clustername",
                "parallelcluster:node-type": "HeadNode",
                "parallelcluster:attributes": "alinux2, awsbatch, [0-9\\.A-Za-z]+, x86_64",
                "parallelcluster:filesystem": "efs=1, multiebs=0, raid=2, fsx=0",
                "parallelcluster:networking": "EFA=NONE",
                # TODO The tag 'parallelcluster:version' is actually included within head node volume tags,
                #  but some refactoring is required to check it within this test.
                # "parallelcluster:version": "[0-9\\.A-Za-z]+",
                "String": "String",
                "two": "two22",
            },
        ),
        (
            "scheduler_plugin.full.yaml",
            {
                "Name": "HeadNode",
                "parallelcluster:cluster-name": "clustername",
                "parallelcluster:node-type": "HeadNode",
                "parallelcluster:attributes": "centos7, plugin, [0-9\\.A-Za-z]+, x86_64",
                "parallelcluster:filesystem": "efs=1, multiebs=1, raid=0, fsx=1",
                "parallelcluster:networking": "EFA=NONE",
                # TODO The tag 'parallelcluster:version' is actually included within head node volume tags,
                #  but some refactoring is required to check it within this test.
                # "parallelcluster:version": "[0-9\\.A-Za-z]+",
                "String": "String",
                "two": "two22",
            },
        ),
    ],
)
def test_head_node_tags_from_instance_definition(mocker, config_file_name, expected_tags):
    mock_aws_api(mocker)
    mock_bucket(mocker)
    mock_bucket_object_utils(mocker)
    input_yaml, cluster = load_cluster_model_from_yaml(config_file_name)
    generated_template, _ = CDKTemplateBuilder().build_cluster_template(
        cluster_config=cluster, bucket=dummy_cluster_bucket(), stack_name="clustername"
    )
    tags = generated_template.get("Resources").get("HeadNode").get("Properties").get("Tags", [])

    actual_tags = {tag["Key"]: tag["Value"] for tag in tags}
    assert_that(actual_tags.keys()).is_equal_to(expected_tags.keys())
    for key in actual_tags.keys():
        assert_that(actual_tags[key]).matches(expected_tags[key])


@freeze_time("2021-01-01T01:01:01")
@pytest.mark.parametrize(
    "config_file_name, imds_support, http_tokens",
    [
        ("slurm.required.yaml", "v1.0", "optional"),
        ("awsbatch.simple.yaml", "v1.0", "optional"),
        ("scheduler_plugin.required.yaml", "v1.0", "optional"),
        ("slurm.required.yaml", None, "optional"),
        ("awsbatch.simple.yaml", None, "optional"),
        ("scheduler_plugin.required.yaml", None, "optional"),
        ("slurm.required.yaml", "v2.0", "required"),
        ("awsbatch.simple.yaml", "v2.0", "required"),
        ("scheduler_plugin.required.yaml", "v2.0", "required"),
    ],
)
def test_cluster_imds_settings(mocker, config_file_name, imds_support, http_tokens):
    mock_aws_api(mocker)
    mock_bucket_object_utils(mocker)

    input_yaml = load_yaml_dict(f"{EXAMPLE_CONFIGS_DIR}/{config_file_name}")
    if imds_support:
        input_yaml["Imds"] = {"ImdsSupport": imds_support}

    cluster = ClusterSchema(cluster_name="clustername").load(input_yaml)

    generated_template, _ = CDKTemplateBuilder().build_cluster_template(
        cluster_config=cluster, bucket=dummy_cluster_bucket(), stack_name="clustername"
    )

    launch_templates = [
        lt for lt_name, lt in generated_template.get("Resources").items() if "LaunchTemplate" in lt_name
    ]
    for launch_template in launch_templates:
        assert_that(
            launch_template.get("Properties").get("LaunchTemplateData").get("MetadataOptions").get("HttpTokens")
        ).is_equal_to(http_tokens)


@pytest.mark.parametrize(
    "config_file_name, vpc_config",
    [
        ("slurm.required.yaml", {"SubnetIds": ["subnet-8e482ce8"], "SecurityGroupIds": ["sg-028d73ae220157d96"]}),
        ("awsbatch.simple.yaml", {"SubnetIds": ["subnet-8e482ce8"], "SecurityGroupIds": ["sg-028d73ae220157d96"]}),
        (
            "scheduler_plugin.required.yaml",
            {"SubnetIds": ["subnet-8e482ce8"], "SecurityGroupIds": ["sg-028d73ae220157d96"]},
        ),
        ("slurm.required.yaml", None),
        ("awsbatch.simple.yaml", None),
        ("scheduler_plugin.required.yaml", None),
    ],
)
def test_cluster_lambda_functions_vpc_config(mocker, config_file_name, vpc_config):
    mock_aws_api(mocker)
    mock_bucket_object_utils(mocker)

    input_yaml = load_yaml_dict(f"{EXAMPLE_CONFIGS_DIR}/{config_file_name}")
    if vpc_config:
        input_yaml["DeploymentSettings"] = input_yaml.get("DeploymentSettings", {})
        input_yaml["DeploymentSettings"]["LambdaFunctionsVpcConfig"] = vpc_config

    cluster = ClusterSchema(cluster_name="clustername").load(input_yaml)

    generated_template, _ = CDKTemplateBuilder().build_cluster_template(
        cluster_config=cluster, bucket=dummy_cluster_bucket(), stack_name="clustername"
    )

    assert_lambdas_have_expected_vpc_config_and_managed_policy(generated_template, vpc_config)


@pytest.mark.parametrize(
    "no_of_compute_resources_per_queue, expected_no_of_nested_stacks, raises_error",
    [
        ({f"queue-{i}": 5 for i in range(10)}, 2, False),
        ({f"queue-{i}": 5 for i in range(20)}, 3, False),
        ({f"queue-{i}": 5 for i in range(30)}, 4, False),
        ({f"queue-{i}": 40 for i in range(1)}, 1, False),
        # 1 queue with 41 compute resources (Exceeds max compute resources per queue - 40)
        ({f"queue-{i}": 41 for i in range(1)}, 1, True),
    ],
    ids=[
        "10 queues with 5 compute resources each",
        "20 queues with 5 compute resources each",
        "30 queues with 5 compute resources each",
        "1 queue with 40 compute resources each",
        "1 queue with 41 compute resources",
    ],
)
def test_cluster_resource_distribution_in_stacks(
    test_datadir,
    pcluster_config_reader,
    capsys,
    mocker,
    no_of_compute_resources_per_queue: int,
    expected_no_of_nested_stacks: int,
    raises_error: bool,
):
    mock_aws_api(mocker)
    "ValueError"
    mock_bucket_object_utils(mocker)
    rendered_config_file = pcluster_config_reader(
        "variable_queue_compute_resources.yaml", no_of_compute_resources_per_queue=no_of_compute_resources_per_queue
    )

    input_yaml, cluster = load_cluster_model_from_yaml(rendered_config_file, test_datadir)

    if raises_error:
        with pytest.raises(ValueError):
            _generate_template(cluster, capsys)
    else:
        cluster_template, assets = _generate_template(cluster, capsys)
        assert_that(expected_no_of_nested_stacks).is_equal_to(len(assets))

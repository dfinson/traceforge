"""Tests for cloud CLI effect classification (AWS, Azure, GCP)."""

import pytest
from functools import partial

from tracemill.classify import classify_shell, get_default_engine


ENGINE = get_default_engine()
cs = partial(classify_shell, engine=ENGINE)


# ── AWS CLI ──


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("aws s3 ls s3://my-bucket", "read_only"),
        ("aws ec2 describe-instances", "read_only"),
        ("aws sts get-caller-identity", "read_only"),
        ("aws logs describe-log-groups", "read_only"),
        ("aws ecr get-login-password", "read_only"),
        ("aws ec2 terminate-instances --instance-ids i-123", "destructive"),
        ("aws s3 rm s3://bucket/key --recursive", "destructive"),
        ("aws cloudformation delete-stack --stack-name prod", "destructive"),
        ("aws rds delete-db-instance --db-instance-identifier prod-db", "destructive"),
        ("aws iam delete-user --user-name bob", "destructive"),
        ("aws s3 cp file.txt s3://bucket/", "mutating"),
        ("aws s3 sync . s3://bucket/", "mutating"),
        ("aws lambda invoke --function-name myFunc out.json", "mutating"),
        ("aws iam create-user --user-name bob", "mutating"),
        ("aws deploy push --application-name app --s3-location s3://b/k", "mutating"),
    ],
)
def test_aws_effect(cmd, expected):
    result = cs(cmd)
    assert result.effect == expected, f"{cmd!r}: got {result.effect!r}"


# ── Azure CLI ──


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("az group list", "read_only"),
        ("az group show --name rg1", "read_only"),
        ("az aks show --name cluster1 --resource-group rg1", "read_only"),
        ("az login", "read_only"),
        ("az group delete --name rg1 --yes", "destructive"),
        ("az vm delete --name vm1 --resource-group rg1 --yes", "destructive"),
        ("az keyvault purge --name kv1", "destructive"),
        ("az vm deallocate --name vm1 --resource-group rg1", "destructive"),
        ("az network nsg delete --name nsg1 --resource-group rg1", "destructive"),
        ("az vm create --name vm1 --resource-group rg1 --image UbuntuLTS", "mutating"),
        ("az webapp deploy --name app1", "mutating"),
        ("az storage blob upload --file f.txt --container c1", "mutating"),
        ("az vm start --name vm1 --resource-group rg1", "mutating"),
        ("az network vnet create --name vnet1 --resource-group rg1", "mutating"),
        ("az role assignment create --assignee user@dom.com --role Reader", "mutating"),
    ],
)
def test_az_effect(cmd, expected):
    result = cs(cmd)
    assert result.effect == expected, f"{cmd!r}: got {result.effect!r}"


# ── GCP gcloud ──


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("gcloud compute instances list", "read_only"),
        ("gcloud compute instances describe vm1", "read_only"),
        ("gcloud auth list", "read_only"),
        ("gcloud config set project my-proj", "read_only"),
        ("gcloud compute instances delete vm1 --zone us-east1-b", "destructive"),
        ("gcloud container clusters delete my-cluster", "destructive"),
        ("gcloud secrets delete my-secret", "destructive"),
        ("gcloud pubsub topics delete my-topic", "destructive"),
        ("gcloud sql instances delete my-db", "destructive"),
        ("gcloud storage rm gs://bucket/obj", "destructive"),
        ("gcloud compute instances create vm1 --zone us-east1-b", "mutating"),
        ("gcloud run deploy my-service --image gcr.io/proj/img", "mutating"),
        ("gcloud pubsub topics create my-topic", "mutating"),
        ("gcloud functions deploy my-func --runtime python312", "mutating"),
        ("gcloud storage cp file gs://bucket/", "mutating"),
    ],
)
def test_gcloud_effect(cmd, expected):
    result = cs(cmd)
    assert result.effect == expected, f"{cmd!r}: got {result.effect!r}"


# ── gsutil (legacy) ──


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("gsutil ls gs://my-bucket", "read_only"),
        ("gsutil cat gs://bucket/key", "read_only"),
        ("gsutil cp file.txt gs://bucket/", "mutating"),
        ("gsutil mv gs://bucket/a gs://bucket/b", "mutating"),
        ("gsutil rm gs://bucket/key", "destructive"),
        ("gsutil rb gs://empty-bucket", "destructive"),
    ],
)
def test_gsutil_effect(cmd, expected):
    result = cs(cmd)
    assert result.effect == expected, f"{cmd!r}: got {result.effect!r}"


# ── Service name / verb prefix collision tests ──


@pytest.mark.parametrize(
    "cmd,expected",
    [
        # 'connect' is an AWS service name AND a verb prefix — service should be skipped
        ("aws connect describe-instance --instance-id i-123", "read_only"),
        ("aws connect list-users --instance-id i-123", "read_only"),
        ("aws connect delete-user --instance-id i-123 --user-id u-1", "destructive"),
        # 'deploy' is an AWS service name (legacy codedeploy alias)
        ("aws deploy list-deployments --application-name app", "read_only"),
        ("aws deploy get-deployment --deployment-id d-123", "read_only"),
        # 'configure' is a built-in command
        ("aws configure list", "read_only"),
        ("aws configure set region us-east-1", "mutating"),
    ],
)
def test_service_name_collision(cmd, expected):
    result = cs(cmd)
    assert result.effect == expected, f"{cmd!r}: got {result.effect!r}"


# ── Batch verbs ──


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("aws dynamodb batch-get-item --request-items file://req.json", "read_only"),
        ("aws dynamodb batch-write-item --request-items file://req.json", "mutating"),
        ("aws sesv2 batch-delete-suppressed-destinations", "destructive"),
    ],
)
def test_aws_batch_verbs(cmd, expected):
    result = cs(cmd)
    assert result.effect == expected, f"{cmd!r}: got {result.effect!r}"


# ── Deep nesting (3+ positional levels) ──


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("aws ec2 describe-security-group-rules --security-group-id sg-123", "read_only"),
        ("az network vnet subnet delete --name sub1 --vnet-name v1 --resource-group rg", "destructive"),
        ("az network vnet subnet create --name sub1 --vnet-name v1 --resource-group rg", "mutating"),
        ("gcloud compute firewall-rules delete rule1", "destructive"),
        ("gcloud compute firewall-rules list", "read_only"),
        ("gcloud container clusters get-credentials my-cluster", "read_only"),
    ],
)
def test_deep_nesting(cmd, expected):
    result = cs(cmd)
    assert result.effect == expected, f"{cmd!r}: got {result.effect!r}"


# ── Fallback behavior (unknown verbs/services) ──


@pytest.mark.parametrize(
    "cmd,expected",
    [
        ("aws ec2 some-unknown-verb --id i-123", None),
        ("az foobar unknown-cmd --name x", None),
        ("gcloud compute things frobnicate", None),
        ("aws", None),
        ("az", None),
        ("gcloud", None),
    ],
)
def test_unknown_verb_fallback(cmd, expected):
    result = cs(cmd)
    assert result.effect == expected, f"{cmd!r}: got {result.effect!r}"


# ── Real-world agent transcript patterns ──


@pytest.mark.parametrize(
    "cmd,expected",
    [
        # Piped commands
        ("aws s3 ls | grep backup", "read_only"),
        # Environment variable prefix
        ("AWS_PROFILE=prod aws ec2 describe-instances", "read_only"),
        # Flags with verb-like words in values
        ("aws ssm put-parameter --name /delete/path --value secret", "mutating"),
        # Long flag chains
        ("az vm create --name vm1 --resource-group rg1 --image UbuntuLTS --size Standard_D2s_v3", "mutating"),
        # gcloud with --format (doesn't affect effect)
        ("gcloud compute instances list --format=json", "read_only"),
        # gcloud with --quiet on delete (still destructive)
        ("gcloud compute instances delete vm1 --project my-proj --zone us-east1-b --quiet", "destructive"),
        # aws with --output flag
        ("aws ec2 describe-instances --output json", "read_only"),
        # presign (read-only, generates URL)
        ("aws s3 presign s3://bucket/key", "read_only"),
    ],
)
def test_real_world_patterns(cmd, expected):
    result = cs(cmd)
    assert result.effect == expected, f"{cmd!r}: got {result.effect!r}"


# ── Scope routing verification ──


@pytest.mark.parametrize(
    "cmd,expected_scope",
    [
        ("aws ec2 describe-instances", "configuration.infrastructure"),
        ("aws s3 ls", "configuration.infrastructure"),
        ("az group list", "configuration.infrastructure"),
        ("gcloud compute instances list", "configuration.infrastructure"),
    ],
)
def test_scope_routing(cmd, expected_scope):
    result = cs(cmd)
    assert expected_scope in result.scope, f"{cmd!r}: scope={result.scope!r}"

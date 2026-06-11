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

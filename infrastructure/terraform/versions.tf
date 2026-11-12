terraform {
  required_version = ">= 1.10, < 2.0"
  required_providers {
    hcloud = { source = "hetznercloud/hcloud", version = "~> 1.70.0" }
  }
  # Supply non-secret bucket/key/endpoint settings via -backend-config. Credentials
  # and the SSE-C key come only from AWS_* environment variables, never tfvars.
  backend "s3" {}
}
provider "hcloud" {} # HCLOUD_TOKEN; deliberately not a Terraform variable.

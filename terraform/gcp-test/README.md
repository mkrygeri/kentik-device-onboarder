# GCP test environment

Spins up a Rocky Linux 9 GCE VM that installs the published `kentik-device-onboarder` `.rpm`, fetches Kentik credentials from Google Secret Manager, installs the Kentik universal agent (which provides the local healthcheck endpoint), and starts the onboarder service. Useful for validating the v1.1.0 cloud auto-detect / reverse-DNS behavior end-to-end on real GCP infrastructure.

## Prerequisites

- `terraform >= 1.5` and `gcloud` installed locally.
- A GCP project with these APIs enabled:
  ```bash
  gcloud services enable compute.googleapis.com secretmanager.googleapis.com iap.googleapis.com
  ```
- Application-default credentials with permission to create VMs, service accounts, IAM bindings, and firewall rules:
  ```bash
  gcloud auth application-default login
  ```

## One-time secret setup

Create the two secrets the VM will read at boot. Values are never written to Terraform state.

```bash
printf '%s' "$KENTIK_EMAIL"     | gcloud secrets create kentik-api-email \
    --data-file=- --replication-policy=automatic
printf '%s' "$KENTIK_API_TOKEN" | gcloud secrets create kentik-api-token \
    --data-file=- --replication-policy=automatic
```

## Deploy

```bash
cd terraform/gcp-test
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars — at minimum set project_id and the two secret IDs

terraform init
terraform apply
```

After `apply` finishes, Terraform prints helper commands:

```bash
# SSH in via IAP (no public IP needed)
$(terraform output -raw ssh_command)

# Tail the onboarder service logs
$(terraform output -raw logs_command)

# Inspect the bootstrap startup-script log
$(terraform output -raw startup_log_command)
```

## What the VM does on boot

1. Pulls the Kentik email/token from Secret Manager via the instance metadata server.
2. Installs the Kentik universal agent via `curl -fsSL <install_url> | sh` (configurable via `universal_agent_install_url`; the default token in the example is for the Kentik demo project and should be replaced with your own).
3. Downloads and installs the `.rpm` from `package_url` (defaults to the v1.1.1 GitHub Release asset).
4. Writes credentials and `KENTIK_ONBOARDER_DNS_SERVER=auto` into `/etc/kentik-device-onboarder/onboarder.env`.
5. Runs `kentik_device_onboarder.py --verify` and logs the result.
6. Enables and starts `kentik-device-onboarder.service`.

## Validating the cloud auto-detect

SSH in and inspect:

```bash
sudo journalctl -u kentik-device-onboarder.service | grep -iE 'dns|resolver|cloud'
sudo systemctl status kentik-device-onboarder.service
```

You should see a log line indicating that the GCE metadata DNS resolver (`169.254.169.254`) was selected.

## Tear down

```bash
terraform destroy
```

The Secret Manager secrets are **not** managed by this module, so they survive `destroy` and can be reused.

## Networking notes

By default the VM is given an **ephemeral external IP** so the startup script can reach `dnf` repos, the Kentik universal-agent installer, GitHub Releases, and the Kentik API. SSH still goes through IAP (the firewall rule only allows IAP source ranges on port 22).

If your security policy requires the VM to have **no public IP**, set both:

```hcl
assign_public_ip = false
create_cloud_nat = true
```

`create_cloud_nat = true` provisions a regional Cloud Router + Cloud NAT so outbound traffic still works. (Cloud NAT costs a few cents per hour while running — destroy when done.)

## Cost note

The default `e2-small` instance + 20 GB `pd-balanced` boot disk runs in the cents-per-hour range. Don't leave it running.

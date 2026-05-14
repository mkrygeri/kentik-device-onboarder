variable "project_id" {
  description = "GCP project to deploy the test VM into."
  type        = string
}

variable "region" {
  description = "GCP region."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "GCP zone."
  type        = string
  default     = "us-central1-a"
}

variable "name_prefix" {
  description = "Prefix applied to the VM, service account, and firewall rule."
  type        = string
  default     = "onboarder-test"
}

variable "machine_type" {
  description = "GCE machine type."
  type        = string
  default     = "e2-small"
}

variable "boot_image" {
  description = <<-EOT
    Boot image. Defaults to Rocky Linux 9 (RHEL-compatible) which matches the
    .rpm we ship. Other RHEL-family images that work: rocky-linux-cloud/
    rocky-linux-9-optimized-gcp, almalinux-cloud/almalinux-9,
    rhel-cloud/rhel-9.
  EOT
  type        = string
  default     = "rocky-linux-cloud/rocky-linux-9"
}

variable "assign_public_ip" {
  description = <<-EOT
    Attach an ephemeral external IP to the VM. Defaults to true because the
    startup script needs outbound internet (apt, Docker Hub, GitHub Releases,
    Kentik API). Set to false only if you also enable Cloud NAT via
    `create_cloud_nat = true`, or if your project's default VPC already has
    NAT/PSC routing for these endpoints.
  EOT
  type        = bool
  default     = true
}

variable "create_cloud_nat" {
  description = <<-EOT
    If true, create a regional Cloud Router + Cloud NAT so a VM with no
    external IP can still reach the public internet. Use together with
    `assign_public_ip = false`. NAT resources cost roughly a few cents per
    hour while running.
  EOT
  type        = bool
  default     = false
}

variable "package_url" {
  description = <<-EOT
    URL of the .rpm to install. Defaults to the v1.1.4 GitHub Release asset.
    Override with a newer tag or a self-hosted artifact URL when testing.
  EOT
  type        = string
  default     = "https://github.com/mkrygeri/kentik-device-onboarder/releases/download/v1.1.4/kentik-device-onboarder-1.1.4-1.noarch.rpm"
}

variable "kentik_email_secret_id" {
  description = <<-EOT
    Fully-qualified Secret Manager secret ID holding KENTIK_API_EMAIL.
    Format: projects/<PROJECT_NUMBER_OR_ID>/secrets/<NAME>
    Create it once with:
      printf '%s' "$KENTIK_EMAIL" | gcloud secrets create kentik-api-email --data-file=- --replication-policy=automatic
  EOT
  type        = string
}

variable "kentik_token_secret_id" {
  description = <<-EOT
    Fully-qualified Secret Manager secret ID holding KENTIK_API_TOKEN.
    Format: projects/<PROJECT_NUMBER_OR_ID>/secrets/<NAME>
  EOT
  type        = string
}

variable "flowpak_id" {
  description = "KENTIK_ONBOARDER_FLOWPAK_ID. Leave 0 to let postinst auto-discover from the plans API."
  type        = number
  default     = 0
}

variable "install_universal_agent" {
  description = <<-EOT
    If true, install the Kentik universal agent on the VM via the official
    install script. The agent provides the local healthcheck endpoint that the
    onboarder polls.
  EOT
  type        = bool
  default     = true
}

variable "universal_agent_install_url" {
  description = <<-EOT
    URL of the Kentik universal-agent install script. The numeric path
    component is the install token / company ID issued by Kentik. Override
    with your own token when deploying outside of the demo project.
  EOT
  type        = string
  default     = "https://grpc.api.kentik.com/install/98837"
}

variable "onboarder_log_level" {
  description = "Log level passed to kentik-device-onboarder."
  type        = string
  default     = "INFO"
}

variable "create_spoofed_ptr_zone" {
  description = <<-EOT
    Create a Cloud DNS private reverse zone with PTR records for the
    spoofed-flow source IPs used by send_spoofed_netflow.py. The zone is
    bound to the default VPC so the test VM resolves the fake IPs to
    fake-device-N.spoofed.test instead of NXDOMAIN.
  EOT
  type        = bool
  default     = true
}

variable "spoofed_src_cidr" {
  description = "CIDR used by send_spoofed_netflow.py (informational; PTRs are written under 0.99.10.in-addr.arpa)."
  type        = string
  default     = "10.99.0.0/24"
}

variable "spoofed_ptr_count" {
  description = "How many PTR records to create starting at 10.99.0.1 (must match send_spoofed_netflow.py --count)."
  type        = number
  default     = 50
}

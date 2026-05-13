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
  description = "Boot image. Debian 12 matches the .deb we ship."
  type        = string
  default     = "debian-cloud/debian-12"
}

variable "assign_public_ip" {
  description = "Attach an ephemeral external IP. Default is false; reach the VM via IAP SSH."
  type        = bool
  default     = false
}

variable "package_url" {
  description = <<-EOT
    URL of the .deb to install. Defaults to the v1.1.1 GitHub Release asset.
    Override with a newer tag or a self-hosted artifact URL when testing.
  EOT
  type        = string
  default     = "https://github.com/mkrygeri/kentik-device-onboarder/releases/download/v1.1.1/kentik-device-onboarder_1.1.1_all.deb"
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

variable "run_kproxy" {
  description = "If true, run the kproxy container so the onboarder has a real healthcheck endpoint."
  type        = bool
  default     = true
}

variable "kproxy_image" {
  description = "Container image for kproxy."
  type        = string
  default     = "kentik/kproxy:latest"
}

variable "kproxy_company_id" {
  description = "Kentik company ID passed to kproxy via -c. Required when run_kproxy = true."
  type        = string
  default     = ""
}

variable "onboarder_log_level" {
  description = "Log level passed to kentik-device-onboarder."
  type        = string
  default     = "INFO"
}

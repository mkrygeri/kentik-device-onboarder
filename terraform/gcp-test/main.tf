// Test environment for kentik-device-onboarder on GCE.
//
// Provisions a single Debian 12 VM that:
//   - Pulls API credentials from Google Secret Manager.
//   - Installs the kentik-device-onboarder .deb from a GitHub release.
//   - Optionally runs the official kproxy container so the onboarder has a
//     real healthcheck endpoint to talk to.
//
// This is intended for ephemeral validation of the cloud auto-detect /
// reverse-DNS behavior. Tear it down with `terraform destroy` when done.

terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 5.0, < 7.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

// ─── Network ────────────────────────────────────────────────────────────────
// Re-use the project's default VPC. Keep the VM private; reach it via IAP.

data "google_compute_network" "default" {
  name = "default"
}

data "google_compute_subnetwork" "default" {
  name   = "default"
  region = var.region
}

resource "google_compute_firewall" "iap_ssh" {
  name        = "${var.name_prefix}-allow-iap-ssh"
  network     = data.google_compute_network.default.name
  description = "Allow SSH from Google IAP for kentik-device-onboarder test VM"

  direction = "INGRESS"
  priority  = 1000

  // IAP TCP forwarding range — see https://cloud.google.com/iap/docs/using-tcp-forwarding
  source_ranges = ["35.235.240.0/20"]
  target_tags   = [local.network_tag]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

// ─── Service account ────────────────────────────────────────────────────────

resource "google_service_account" "vm" {
  account_id   = "${var.name_prefix}-vm"
  display_name = "kentik-device-onboarder test VM"
}

// Grant access to the two secrets that hold Kentik credentials.
resource "google_secret_manager_secret_iam_member" "email_accessor" {
  secret_id = var.kentik_email_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}

resource "google_secret_manager_secret_iam_member" "token_accessor" {
  secret_id = var.kentik_token_secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.vm.email}"
}

// Allow logs to flow to Cloud Logging for easier debugging.
resource "google_project_iam_member" "log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.vm.email}"
}

// ─── VM ─────────────────────────────────────────────────────────────────────

locals {
  network_tag = "${var.name_prefix}-vm"

  startup_script = templatefile("${path.module}/startup.sh", {
    package_url            = var.package_url
    kentik_email_secret_id = var.kentik_email_secret_id
    kentik_token_secret_id = var.kentik_token_secret_id
    flowpak_id             = var.flowpak_id
    run_kproxy             = var.run_kproxy
    kproxy_image           = var.kproxy_image
    kproxy_company_id      = var.kproxy_company_id
    onboarder_log_level    = var.onboarder_log_level
  })
}

resource "google_compute_instance" "vm" {
  name         = "${var.name_prefix}-vm"
  machine_type = var.machine_type
  zone         = var.zone
  tags         = [local.network_tag]

  boot_disk {
    initialize_params {
      image = var.boot_image
      size  = 20
      type  = "pd-balanced"
    }
  }

  network_interface {
    network    = data.google_compute_network.default.self_link
    subnetwork = data.google_compute_subnetwork.default.self_link

    // No external IP — connect over IAP. Flip to true if you want a public IP.
    dynamic "access_config" {
      for_each = var.assign_public_ip ? [1] : []
      content {
        // Ephemeral
      }
    }
  }

  service_account {
    email  = google_service_account.vm.email
    scopes = ["cloud-platform"]
  }

  metadata = {
    enable-oslogin = "TRUE"
    startup-script = local.startup_script
  }

  shielded_instance_config {
    enable_secure_boot          = true
    enable_vtpm                 = true
    enable_integrity_monitoring = true
  }

  // Tearing down and re-creating is the expected workflow for this test rig.
  allow_stopping_for_update = true

  depends_on = [
    google_secret_manager_secret_iam_member.email_accessor,
    google_secret_manager_secret_iam_member.token_accessor,
  ]
}

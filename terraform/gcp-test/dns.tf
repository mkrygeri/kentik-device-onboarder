// ─── Private reverse DNS for spoofed-flow source IPs ───────────────────────
// The send_spoofed_netflow.py traffic generator emits flows from sources in
// var.spoofed_src_cidr (default 10.99.0.0/24). The onboarder does a reverse
// DNS lookup on each unregistered IP to derive a hostname. Without PTR
// records the lookup returns NXDOMAIN and the device is named after its IP,
// which makes it harder to verify the end-to-end flow in test runs.
//
// We create a Cloud DNS *private* reverse zone bound to the default VPC and
// pre-populate PTR records for the first var.spoofed_ptr_count addresses.

resource "google_dns_managed_zone" "spoofed_reverse" {
  count       = var.create_spoofed_ptr_zone ? 1 : 0
  project     = var.project_id
  name        = "${var.name_prefix}-spoofed-reverse"
  dns_name    = "0.99.10.in-addr.arpa."
  description = "Private PTR zone for spoofed-flow test sources (${var.spoofed_src_cidr})"
  visibility  = "private"

  private_visibility_config {
    networks {
      network_url = data.google_compute_network.default.self_link
    }
  }
}

resource "google_dns_record_set" "spoofed_ptr" {
  count        = var.create_spoofed_ptr_zone ? var.spoofed_ptr_count : 0
  project      = var.project_id
  managed_zone = google_dns_managed_zone.spoofed_reverse[0].name
  name         = "${count.index + 1}.0.99.10.in-addr.arpa."
  type         = "PTR"
  ttl          = 300
  rrdatas      = ["fake-device-${count.index + 1}.spoofed.test."]
}

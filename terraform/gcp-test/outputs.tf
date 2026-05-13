output "vm_name" {
  description = "GCE instance name."
  value       = google_compute_instance.vm.name
}

output "vm_zone" {
  description = "GCE zone."
  value       = google_compute_instance.vm.zone
}

output "internal_ip" {
  description = "Primary internal IP of the test VM."
  value       = google_compute_instance.vm.network_interface[0].network_ip
}

output "external_ip" {
  description = "Ephemeral external IP, if assign_public_ip = true. Empty otherwise."
  value = try(
    google_compute_instance.vm.network_interface[0].access_config[0].nat_ip,
    "",
  )
}

output "ssh_command" {
  description = "Convenience command to SSH into the VM via IAP."
  value       = "gcloud compute ssh ${google_compute_instance.vm.name} --zone=${google_compute_instance.vm.zone} --tunnel-through-iap"
}

output "logs_command" {
  description = "Convenience command to tail the onboarder service logs."
  value       = "gcloud compute ssh ${google_compute_instance.vm.name} --zone=${google_compute_instance.vm.zone} --tunnel-through-iap --command='sudo journalctl -u kentik-device-onboarder.service -f'"
}

output "startup_log_command" {
  description = "Convenience command to inspect the startup-script output."
  value       = "gcloud compute ssh ${google_compute_instance.vm.name} --zone=${google_compute_instance.vm.zone} --tunnel-through-iap --command='sudo journalctl -u google-startup-scripts.service --no-pager'"
}

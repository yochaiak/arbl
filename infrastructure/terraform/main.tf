locals {
  nodes = merge(
    { for i in range(3) : "control-${i + 1}" => { role = "control", type = var.control_type, ip = cidrhost(var.subnet_cidr, 10 + i) } },
    { for i in range(3) : "data-${i + 1}" => { role = "data", type = var.data_type, ip = cidrhost(var.subnet_cidr, 20 + i) } },
    { for i in range(2) : "app-${i + 1}" => { role = "app", type = var.application_type, ip = cidrhost(var.subnet_cidr, 30 + i) } }
  )
}
resource "hcloud_network" "cluster" {
  name     = var.name
  ip_range = var.network_cidr
  labels   = { cluster = var.name }
}
resource "hcloud_network_subnet" "cluster" {
  network_id   = hcloud_network.cluster.id
  type         = "cloud"
  network_zone = var.network_zone
  ip_range     = var.subnet_cidr
}
resource "hcloud_placement_group" "roles" {
  for_each = toset(["control", "data", "app"])
  name     = "${var.name}-${each.key}"
  type     = "spread"
  labels   = { cluster = var.name }
}
resource "hcloud_firewall" "cluster" {
  name   = "${var.name}-nodes"
  labels = { cluster = var.name }
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "22"
    source_ips = var.administrator_cidrs
  }
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "6443"
    source_ips = var.administrator_cidrs
  }
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "any"
    source_ips = [var.network_cidr]
  }
  rule {
    direction  = "in"
    protocol   = "udp"
    port       = "any"
    source_ips = [var.network_cidr]
  }
  rule {
    direction  = "in"
    protocol   = "icmp"
    source_ips = var.administrator_cidrs
  }
}
resource "hcloud_server" "fixed" {
  for_each           = local.nodes
  name               = "${var.name}-${each.key}"
  server_type        = each.value.type
  image              = var.image
  location           = var.location
  ssh_keys           = var.ssh_key_ids
  firewall_ids       = [hcloud_firewall.cluster.id]
  placement_group_id = hcloud_placement_group.roles[each.value.role].id
  labels             = { cluster = var.name, role = each.value.role, owner = "terraform" }
  backups            = true # OS only. PVC/database/object backups are separate.
  delete_protection  = true
  rebuild_protection = true
  # Deliberately no token, secret, or user_data in state. Bootstrap is separate.
  network {
    network_id = hcloud_network.cluster.id
    ip         = each.value.ip
  }
  depends_on = [hcloud_network_subnet.cluster]
  lifecycle { prevent_destroy = true }
}
resource "hcloud_load_balancer" "ingress" {
  name               = "${var.name}-ingress"
  load_balancer_type = "lb11"
  location           = var.location
  labels             = { cluster = var.name }
  algorithm { type = "round_robin" }
}
resource "hcloud_load_balancer_network" "ingress" {
  load_balancer_id = hcloud_load_balancer.ingress.id
  network_id       = hcloud_network.cluster.id
  ip               = cidrhost(var.subnet_cidr, 40)
  depends_on       = [hcloud_network_subnet.cluster]
}
resource "hcloud_load_balancer_target" "apps" {
  for_each         = { for k, v in local.nodes : k => v if v.role == "app" }
  load_balancer_id = hcloud_load_balancer.ingress.id
  type             = "server"
  server_id        = hcloud_server.fixed[each.key].id
  use_private_ip   = true
  depends_on       = [hcloud_load_balancer_network.ingress]
}
resource "hcloud_load_balancer_service" "https" {
  proxyprotocol    = true
  load_balancer_id = hcloud_load_balancer.ingress.id
  protocol         = "tcp"
  listen_port      = 443
  destination_port = 30443
  health_check {
    protocol = "tcp"
    port     = 30443
    interval = 10
    timeout  = 5
    retries  = 3
  }
}
resource "hcloud_load_balancer_service" "http" {
  proxyprotocol    = true
  load_balancer_id = hcloud_load_balancer.ingress.id
  protocol         = "tcp"
  listen_port      = 80
  destination_port = 30080
  health_check {
    protocol = "tcp"
    port     = 30080
    interval = 10
    timeout  = 5
    retries  = 3
  }
}

resource "hcloud_network_subnet" "workers" {
  for_each     = { standard = cidrsubnet(var.network_cidr, 4, 1), vendor = cidrsubnet(var.network_cidr, 4, 2) }
  network_id   = hcloud_network.cluster.id
  type         = "cloud"
  network_zone = var.network_zone
  ip_range     = each.value
}

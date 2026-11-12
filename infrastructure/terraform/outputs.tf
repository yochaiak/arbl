output "inventory" {
  value = { for name, server in hcloud_server.fixed : name => {
    id = server.id, address = server.ipv4_address, private_address = local.nodes[name].ip, role = local.nodes[name].role
  } }
}
output "network_id" { value = hcloud_network.cluster.id }
output "firewall_id" { value = hcloud_firewall.cluster.id }
output "ingress_ipv4" { value = hcloud_load_balancer.ingress.ipv4 }
output "capacity_limits" {
  value = { maximum_browser_nodes = var.maximum_browser_nodes, maximum_sessions = 500, fixed_nodes = 8, network_attachment_reserve = 11 }
}

variable "name" {
  type    = string
  default = "abrl"
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,24}$", var.name))
    error_message = "Use a 3–25 character lowercase cluster name."
  }
}
variable "location" {
  type    = string
  default = "fsn1"
}
variable "network_zone" {
  type    = string
  default = "eu-central"
}
variable "image" {
  type    = string
  default = "ubuntu-24.04"
}
variable "control_type" {
  type    = string
  default = "cpx31"
}
variable "application_type" {
  type    = string
  default = "cpx41"
}
variable "data_type" {
  type    = string
  default = "cpx51"
}
variable "ssh_key_ids" {
  type = list(string)
  validation {
    condition     = length(var.ssh_key_ids) > 0
    error_message = "Provide at least one existing administrator SSH key ID."
  }
}
variable "administrator_cidrs" {
  type = list(string)
  validation {
    condition     = length(var.administrator_cidrs) > 0 && alltrue([for c in var.administrator_cidrs : can(cidrhost(c, 0)) && !contains(["0.0.0.0/0", "::/0"], c)])
    error_message = "Restrict administration to explicit IPv4/IPv6 CIDRs."
  }
}
variable "network_cidr" {
  type    = string
  default = "10.20.0.0/16"
}
variable "subnet_cidr" {
  type    = string
  default = "10.20.0.0/20"
}
variable "maximum_browser_nodes" {
  type    = number
  default = 80
  validation {
    condition     = var.maximum_browser_nodes >= 2 && var.maximum_browser_nodes <= 80 && floor(var.maximum_browser_nodes) == var.maximum_browser_nodes
    error_message = "Browser node ceiling must be an integer from 2 to 80."
  }
}

# Stack 1: the CDN origin. Costs cents per month and does not start the
# Databricks trial, so apply this first.
terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
  # Local state keeps the lab simple. A team would use an azurerm backend
  # with state locking; say so in the interview.
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

variable "subscription_id" {
  type        = string
  description = "Azure subscription ID (pay-as-you-go; the Databricks trial is not available on a Free Trial subscription)."
}

variable "location" {
  type    = string
  default = "eastus2"
}

variable "sas_expiry" {
  type        = string
  default     = "2026-12-31"
  description = "Date the SAS tokens stop working. Short-lived on purpose."
}

resource "random_string" "suffix" {
  length  = 6
  upper   = false
  special = false
}

resource "azurerm_resource_group" "this" {
  name     = "rg-weather-edge"
  location = var.location
  tags     = { project = "weather-edge", purpose = "lab" }
}

resource "azurerm_storage_account" "origin" {
  name                            = "stweatheredge${random_string.suffix.result}"
  resource_group_name             = azurerm_resource_group.this.name
  location                        = azurerm_resource_group.this.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  allow_nested_items_to_be_public = false # the origin is private; only the edge may read it
  tags                            = azurerm_resource_group.this.tags
}

resource "azurerm_storage_container" "weather" {
  name                  = "weather"
  storage_account_id    = azurerm_storage_account.origin.id
  container_access_type = "private"
}

# Two tokens, two blast radii: the edge and the agent can only read,
# the pipeline can only write.
data "azurerm_storage_account_blob_container_sas" "read" {
  connection_string = azurerm_storage_account.origin.primary_connection_string
  container_name    = azurerm_storage_container.weather.name
  https_only        = true
  start             = "2026-10-01"
  expiry            = var.sas_expiry

  permissions {
    read   = true
    list   = true
    add    = false
    create = false
    write  = false
    delete = false
  }
}

data "azurerm_storage_account_blob_container_sas" "write" {
  connection_string = azurerm_storage_account.origin.primary_connection_string
  container_name    = azurerm_storage_container.weather.name
  https_only        = true
  start             = "2026-10-01"
  expiry            = var.sas_expiry

  permissions {
    read   = false
    list   = false
    add    = true
    create = true
    write  = true
    delete = false
  }
}

output "resource_group_name" {
  value = azurerm_resource_group.this.name
}

output "location" {
  value = azurerm_resource_group.this.location
}

output "container_url" {
  value = "${azurerm_storage_account.origin.primary_blob_endpoint}${azurerm_storage_container.weather.name}"
}

output "read_sas" {
  value     = trimprefix(data.azurerm_storage_account_blob_container_sas.read.sas, "?")
  sensitive = true
}

output "write_sas" {
  value     = trimprefix(data.azurerm_storage_account_blob_container_sas.write.sas, "?")
  sensitive = true
}

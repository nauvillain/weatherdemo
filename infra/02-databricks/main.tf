# Stack 2: the Databricks workspace. APPLYING THIS STARTS THE 14-DAY TRIAL.
# Hold it until the origin and the edge already work.
terraform {
  required_version = ">= 1.6"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    databricks = {
      source  = "databricks/databricks"
      version = "~> 1.50"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

variable "subscription_id" {
  type = string
}

# Reads stack 1's outputs, so the SAS tokens never get copied by hand.
data "terraform_remote_state" "origin" {
  backend = "local"
  config  = { path = "${path.module}/../01-origin/terraform.tfstate" }
}

resource "azurerm_databricks_workspace" "this" {
  name                = "dbw-weather-edge"
  resource_group_name = data.terraform_remote_state.origin.outputs.resource_group_name
  location            = data.terraform_remote_state.origin.outputs.location
  sku                 = "trial" # Premium features, 14 days of free DBUs
  tags                = { project = "weather-edge", purpose = "lab" }
}

# Authenticates with your `az login` session.
provider "databricks" {
  host                        = azurerm_databricks_workspace.this.workspace_url
  azure_workspace_resource_id = azurerm_databricks_workspace.this.id
}

resource "databricks_secret_scope" "weather" {
  name = "weather-edge"
}

resource "databricks_secret" "container_url" {
  scope        = databricks_secret_scope.weather.name
  key          = "origin_container_url"
  string_value = data.terraform_remote_state.origin.outputs.container_url
}

resource "databricks_secret" "write_sas" {
  scope        = databricks_secret_scope.weather.name
  key          = "origin_write_sas"
  string_value = data.terraform_remote_state.origin.outputs.write_sas
}

output "workspace_url" {
  value = "https://${azurerm_databricks_workspace.this.workspace_url}"
}

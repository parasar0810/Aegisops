variable "aws_region" {
  type    = string
  default = "ap-south-1"
}
variable "project_name" {
  type    = string
  default = "aegisops"
}
variable "instance_type" {
  type        = string
  default     = "t4g.small"
  description = "2 GiB Graviton, Free-plan eligible, $0.0112/h in ap-south-1. Measured peak ~0.9 GiB containers + ~0.3 GiB OS; 1 GiB is too tight. x86 fallback: t3.small."
}
variable "disk_gb" {
  type    = number
  default = 20
}
variable "allowed_cidrs" {
  type        = list(string)
  description = "Who may reach AegisOps on port 8000, e.g. [\"203.0.113.7/32\"]. Add your website's egress IP later."
}
variable "bundle_path" {
  type        = string
  default     = "../../../dist/aegisops-share.zip"
  description = "Built with: python scripts/package_share.py"
}
variable "gemini_api_key" {
  type      = string
  default   = ""
  sensitive = true
}
variable "budget_email" {
  type        = string
  default     = ""
  description = "Email for AWS Budget alerts at 50/75/90/100%. Empty = no budget created."
}
variable "monthly_budget_usd" {
  type    = string
  default = "10"
}

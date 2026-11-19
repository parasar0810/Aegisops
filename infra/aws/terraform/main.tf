# AegisOps on a single EC2 instance (Docker Compose), ap-south-1, minimum cost.
# No ALB, NAT gateway, Elastic IP, RDS, EKS or CloudWatch log groups.
# Access: AegisOps port 8000 from allowed CIDRs only; admin shell and dashboards via SSM Session Manager.
terraform {
  required_version = ">= 1.6"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.60" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "aws" {
  region = var.aws_region
  default_tags { tags = { Project = var.project_name, ManagedBy = "terraform" } }
}

data "aws_caller_identity" "me" {}
data "aws_vpc" "default" { default = true }
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}
locals {
  name   = var.project_name
  prefix = "/${var.project_name}"
  arm    = can(regex("^[a-z]+[0-9]+g", var.instance_type)) # Graviton families: t4g, m7g, ...
}

data "aws_ssm_parameter" "al2023" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-${local.arm ? "arm64" : "x86_64"}"
}

# ── Secrets (SSM Parameter Store standard tier: free) ───────────────────────
resource "random_password" "jwt" {
  length  = 48
  special = false
}
resource "random_password" "operator" {
  length  = 20
  special = false
}
resource "random_password" "grafana" {
  length  = 20
  special = false
}
resource "random_password" "hook" { # ShopFront remediation (ops) token
  length  = 32
  special = false
}
resource "random_password" "demo_admin" { # demo-target remediation token
  length  = 32
  special = false
}
resource "random_password" "shop_fault" { # ShopFront demo fault-injection token
  length  = 32
  special = false
}
resource "random_password" "shop_db" {
  length  = 32
  special = false
}

resource "aws_ssm_parameter" "secrets" {
  for_each = toset(concat(["JWT_SECRET_KEY", "DEMO_PASSWORD", "GRAFANA_ADMIN_PASSWORD", "AEGIS_HOOK_TOKEN_SHOPFRONT",
    "DEMO_ADMIN_TOKEN", "SHOPFRONT_FAULT_TOKEN", "SHOPFRONT_DB_PASSWORD"],
  nonsensitive(var.gemini_api_key != "") ? ["GEMINI_API_KEY"] : []))
  name = "${local.prefix}/${each.key}"
  type = "SecureString"
  value = {
    JWT_SECRET_KEY             = random_password.jwt.result
    DEMO_PASSWORD              = random_password.operator.result
    GRAFANA_ADMIN_PASSWORD     = random_password.grafana.result
    AEGIS_HOOK_TOKEN_SHOPFRONT = random_password.hook.result
    DEMO_ADMIN_TOKEN           = random_password.demo_admin.result
    SHOPFRONT_FAULT_TOKEN      = random_password.shop_fault.result
    SHOPFRONT_DB_PASSWORD      = random_password.shop_db.result
    GEMINI_API_KEY             = var.gemini_api_key
  }[each.key]
}

# ── Code bundle (private S3 object, read only by the instance role) ─────────
resource "aws_s3_bucket" "bundle" {
  bucket_prefix = "${local.name}-bundle-"
  force_destroy = true
}
resource "aws_s3_bucket_public_access_block" "bundle" {
  bucket                  = aws_s3_bucket.bundle.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_object" "bundle" {
  bucket = aws_s3_bucket.bundle.id
  key    = "aegisops-share.zip"
  source = var.bundle_path
  etag   = filemd5(var.bundle_path)
}

# ── IAM: SSM access + read own secrets and bundle. Nothing else. ────────────
data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "instance" {
  name               = "${local.name}-instance"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
data "aws_iam_policy_document" "app" {
  statement {
    actions   = ["ssm:GetParametersByPath", "ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.me.account_id}:parameter${local.prefix}*"]
  }
  statement {
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.bundle.arn}/*"]
  }
}
resource "aws_iam_role_policy" "app" {
  role   = aws_iam_role.instance.id
  policy = data.aws_iam_policy_document.app.json
}
resource "aws_iam_instance_profile" "instance" {
  name = "${local.name}-instance"
  role = aws_iam_role.instance.name
}

# ── Network: only port 8000, only from allowed CIDRs. No SSH port. ──────────
resource "aws_security_group" "app" {
  name        = "${local.name}-app"
  description = "AegisOps API/console from allowed CIDRs only" # changing this forces SG replacement
  vpc_id      = data.aws_vpc.default.id
  ingress {
    description = "HTTPS console/API/webhooks"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.allowed_cidrs
  }
  ingress {
    description = "HTTP: ACME challenge and redirect to HTTPS only (app never served on 80)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    description = "Outbound: package installs, SSM, monitored apps, LLM APIs"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# ── Instance ────────────────────────────────────────────────────────────────
resource "aws_instance" "app" {
  ami                         = data.aws_ssm_parameter.al2023.value
  instance_type               = var.instance_type
  subnet_id                   = data.aws_subnets.default.ids[0]
  vpc_security_group_ids      = [aws_security_group.app.id]
  iam_instance_profile        = aws_iam_instance_profile.instance.name
  associate_public_ip_address = true # needed without NAT; auto-assigned, released on stop
  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }
  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.disk_gb
    encrypted             = true
    delete_on_termination = true
  }
  credit_specification {
    cpu_credits = "standard" # no surprise "unlimited" CPU credit charges
  }
  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    region     = var.aws_region
    prefix     = local.prefix
    bucket     = aws_s3_bucket.bundle.id
    object_key = aws_s3_object.bundle.key
    compose    = local.arm ? "aarch64" : "x86_64"
  })
  tags       = { Name = local.name }
  depends_on = [aws_ssm_parameter.secrets, aws_iam_role_policy.app]
  lifecycle {
    ignore_changes = [ami, user_data] # no replacement for new AMIs; user_data only matters at first boot
  }
}

# Stable public IP: keeps the HTTPS hostname and webhook URL fixed across stop/start.
# Same $0.005/h as the auto-assigned IPv4 while attached (also billed while the instance is stopped).
resource "aws_eip" "app" {
  instance = aws_instance.app.id
  domain   = "vpc"
  tags     = { Name = local.name }
}

# ── Billing alerts (AWS Budgets: first two budgets are free) ────────────────
resource "aws_budgets_budget" "monthly" {
  count        = var.budget_email == "" ? 0 : 1
  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = var.monthly_budget_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  dynamic "notification" {
    for_each = [50, 75, 90, 100]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.budget_email]
    }
  }
}

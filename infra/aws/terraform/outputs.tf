output "account_id" { value = data.aws_caller_identity.me.account_id }
output "instance_id" { value = aws_instance.app.id }
output "public_ip" { value = aws_eip.app.public_ip }
output "domain" { value = "${replace(aws_eip.app.public_ip, ".", "-")}.sslip.io" }
output "console_url" { value = "https://${replace(aws_eip.app.public_ip, ".", "-")}.sslip.io/" }
output "ip_url" { value = "http://${aws_eip.app.public_ip}/ (redirects to HTTPS)" }
output "webhook_url" { value = "https://${replace(aws_eip.app.public_ip, ".", "-")}.sslip.io/api/v1/incidents/webhook" }
output "operator_username" { value = "analyst" }
output "operator_password_cmd" {
  value = "aws ssm get-parameter --region ${var.aws_region} --with-decryption --name ${local.prefix}/DEMO_PASSWORD --query Parameter.Value --output text"
}
output "ssm_shell" { value = "aws ssm start-session --region ${var.aws_region} --target ${aws_instance.app.id}" }
output "grafana_tunnel" {
  value = "aws ssm start-session --region ${var.aws_region} --target ${aws_instance.app.id} --document-name AWS-StartPortForwardingSession --parameters portNumber=3000,localPortNumber=3000"
}

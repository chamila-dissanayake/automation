variable "env" {type = map(string)}
variable "tags" { type = map(string)}

variable "ec2ops-lambda" {
    type = map(string)
    default = {}
}

variable "cloudfront" {
    type = map(string)
    default = {}
}

variable "role" {
  default = "ec2ops-lambda"
}

locals {
    defaults = {
        name                = ""
        memory_size         = "128"
        timeout             = "900"
        script_name         = "ec2ops"
        slack_webhook       = ""
    }
    lambda = merge(
        local.defaults,
        var.ec2ops-lambda
    )
}
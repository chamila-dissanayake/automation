variable "env" { type = map(string) }
variable "tags" { type = map(string) }

variable "lambda" {
  type    = map(string)
  default = {}
}

locals {
  defaults = {
    name               = "publish-sns-to-slack"
    memory_size        = "128"
    timeout            = "900"
    runtime            = "python3.9"
    script_name        = "function"
    log_retention_days = 7
    slack_webhook      = ""
  }
  lambda = merge(local.defaults, var.lambda)
}

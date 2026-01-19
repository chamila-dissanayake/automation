variable "env" {type = map(string)}

variable "tags" { type = map(string)}

variable "lambda" {
    type = map(string)
    default = {}
}

locals {
    defaults = {
      memory_size   = "128"
      timeout       = "900"
      runtime       = "python3.8"
      script_name   = "resizerds"
      slack_webhook = ""
    }
    lambda = merge(
        local.defaults,
        var.lambda
    )
}

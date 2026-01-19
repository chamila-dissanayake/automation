terraform {
  required_version = ">=1.7.3"
  required_providers {
    aws = "~> 5.0.0"
  }
}

provider "aws" {
  region = var.env.region
}

data "aws_caller_identity" "current" {}

# Lambda ECS Task Cloudwatch Log
resource "aws_cloudwatch_log_group" "cloudwatch_log_group" {
  name              = "/aws/lambda/${var.env.environment}-${local.lambda.name}"
  retention_in_days = local.lambda.log_retention_days
}

resource "aws_iam_role" "snstoslack_lambda" {
  name = "${var.env.environment}-${local.lambda.name}"
  path = "/service-role/"

  assume_role_policy = <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Action": "sts:AssumeRole",
      "Principal": {
        "Service": [
          "lambda.amazonaws.com"
        ]
      },
      "Effect": "Allow",
      "Sid": ""
    }
  ]
}
EOF
}

data "template_file" "cloudwatch_lambda_template" {
  template = file("${path.module}/policies/cloudwatch.json")
  vars = {
    account_id = data.aws_caller_identity.current.account_id
    region     = var.env.region
    env_name   = var.env.environment
  }
}

resource "aws_iam_policy" "cloudwatch_lambda_policy" {
  name        = "${var.env.environment}-${local.lambda.name}-cloudwatch-lambda-policy"
  description = "Allows to access to lambda"
  policy      = data.template_file.cloudwatch_lambda_template.rendered
}

resource "aws_iam_role_policy_attachment" "cloudwatch_lambda_attachment" {
  role       = aws_iam_role.snstoslack_lambda.id
  policy_arn = aws_iam_policy.cloudwatch_lambda_policy.arn
}

resource "aws_iam_role_policy_attachment" "rds_lambda_attachment" {
  role       = aws_iam_role.snstoslack_lambda.id
  policy_arn = "arn:aws:iam::aws:policy/AmazonSNSReadOnlyAccess"
}


####### Lambda configuration ################################
data "archive_file" "lambda_zip" {
  type        = "zip"
  source_file = "${path.module}/source/${local.lambda.script_name}.py"
  output_path = "${path.module}/source/${local.lambda.script_name}.zip"
}

resource "aws_lambda_function" "rsnstoslack_lambda" {
  filename         = data.archive_file.lambda_zip.output_path
  function_name    = "${var.env.environment}-${local.lambda.name}"
  role             = aws_iam_role.snstoslack_lambda.arn
  memory_size      = local.lambda.memory_size
  timeout          = local.lambda.timeout
  package_type     = "Zip"
  runtime          = local.lambda.runtime
  handler          = "${local.lambda.script_name}.lambda_handler"
  publish          = true
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256


  environment {
    variables = {
      slack_webhook = local.lambda.slack_webhook
    }
  }

  tags = var.tags
}
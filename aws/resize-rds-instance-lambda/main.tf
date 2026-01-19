provider "aws" {
  region = var.env.region
}

data "aws_caller_identity" "current" {}

# Lambda ECS Task Cloudwatch Log
resource "aws_cloudwatch_log_group" "cloudwatch_log_group" {
  name              = "/aws/lambda/${var.env.environment}-${local.lambda.name}"
  retention_in_days = 14
}

resource "aws_iam_role" "resize_lambda" {
  name = "${var.env.environment}-${local.lambda.name}-resize-lambda"
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
  role       = aws_iam_role.resize_lambda.id
  policy_arn = aws_iam_policy.cloudwatch_lambda_policy.arn
}

data "template_file" "rds_lambda_template" {
  template = file("${path.module}/policies/rds.json")
  vars = {
    account_id = data.aws_caller_identity.current.account_id
    region     = var.env.region
    env_name   = var.env.environment
  }
}

resource "aws_iam_policy" "rds_lambda_policy" {
  name        = "${var.env.environment}-${local.lambda.name}-lambda-policy"
  description = "Allows to access to lambda"
  policy      = data.template_file.rds_lambda_template.rendered
}

resource "aws_iam_role_policy_attachment" "rds_lambda_attachment" {
  role       = aws_iam_role.resize_lambda.id
  policy_arn = aws_iam_policy.rds_lambda_policy.arn
}

####### Lambda configuration ################################
data "archive_file" "lambda_zip" {
  type       = "zip"
  source_file = "${path.module}/source/${local.lambda.script_name}.py"
  output_path = "${path.module}/source/${local.lambda.script_name}.zip"
}

resource "aws_lambda_function" "resize_lambda" {
  filename      = data.archive_file.lambda_zip.output_path
  function_name = "${var.env.environment}-${local.lambda.name}"
  role          = aws_iam_role.resize_lambda.arn
  memory_size   = local.lambda.memory_size
  timeout       = local.lambda.timeout
  package_type  = "Zip"
  runtime       = local.lambda.runtime
  handler       = "${local.lambda.script_name}.lambda_handler"
  publish       = true
  source_code_hash = data.archive_file.lambda_zip.output_base64sha256


  environment {
    variables = {
      dbinstance_type = local.lambda.dbinstance_type
      env_name        = var.env.environment
      region          = var.env.region
      slack_webhook   = local.lambda.slack_webhook
      EnablePerformanceInsightsFlag = local.lambda.EnablePerformanceInsightsFlag
      db_param_group_name           = local.lambda.db_param_group_name
    }
  }

  tags = var.tags
}

# Lambda ECS Task Cloudwatch Event rule
resource "aws_cloudwatch_event_rule" "lambda_rds_resize_rule" {
  name                = "${var.env.environment}-${local.lambda.name}-lambda-rds-resize"
  description         = "rule to either scale up or scale down"
  schedule_expression = local.lambda.cron_expression
}

resource "aws_cloudwatch_event_target" "lambda_rds_resize" {
  rule      = aws_cloudwatch_event_rule.lambda_rds_resize_rule.name
  target_id = "lambda"
  arn       = aws_lambda_function.resize_lambda.arn
}

resource "aws_lambda_permission" "cloudwatch_lambda_ecs_restart" {
  statement_id  = "AllowExecutionFromCloudWatch"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.resize_lambda.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.lambda_rds_resize_rule.arn
}

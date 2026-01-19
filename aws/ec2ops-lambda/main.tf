provider "aws" {
  region = var.env.region

  default_tags {
    tags = merge(var.tags, { Name = "${var.tags.Name}-${var.role}" })
  }
}

data "aws_caller_identity" "current" {}

resource "aws_cloudwatch_log_group" "ec2ops_log_group" {
  name              = "/aws/lambda/${var.env.environment}-${var.ec2ops-lambda.name}"
  retention_in_days = 14
}

resource "aws_iam_role" "ec2ops" {
  name = "${var.env.environment}-${var.ec2ops-lambda.name}-lambda"
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
      "Sid": "1"
    }
  ]
}
EOF

  #tags = var.tags
}

data "template_file" "cloudwatch_lambda_template" {
  template = file("${path.module}/policies/cloudwatch.json")
  vars = {
    account_id = data.aws_caller_identity.current.account_id
    region        = var.env.region
    env_name      = var.env.environment
    function_name = "${var.env.environment}-${var.ec2ops-lambda.name}"
  }
}

resource "aws_iam_policy" "cloudwatch_lambda_policy" {
  name        = "${var.env.environment}-${local.lambda.name}-cloudwatch-policy"
  description = "Allows to access to lambda"
  policy      = data.template_file.cloudwatch_lambda_template.rendered
}

resource "aws_iam_role_policy_attachment" "cloudwatch_lambda_attachment" {
  role       = aws_iam_role.ec2ops.id
  policy_arn = aws_iam_policy.cloudwatch_lambda_policy.arn
}

data "template_file" "ecs_lambda_template" {
  template = file("${path.module}/policies/ec2ops_lambda.json")
  vars = {
    account_id    = data.aws_caller_identity.current.account_id
    region        = var.env.region
    env_name      = var.env.environment
    function_name = "${var.env.environment}-${var.ec2ops-lambda.name}"
  }
}

resource "aws_iam_policy" "ecs_lambda_policy" {
  name        = "${var.env.environment}-${var.ec2ops-lambda.name}-lambda-policy"
  description = "Allows to access to lambda"
  policy      = data.template_file.ecs_lambda_template.rendered
}


resource "aws_iam_role_policy_attachment" "ecs_lambda_attachment" {
  role       = aws_iam_role.ec2ops.id
  policy_arn = aws_iam_policy.ecs_lambda_policy.arn
}

####### Lambda configuration ################################
data "archive_file" "ec2ops_zip" {
  type        = "zip"
  source_file = "${path.module}/source/${local.lambda.script_name}.py"
  output_path = "${path.module}/source/${local.lambda.script_name}.zip"
}

resource "aws_lambda_function" "ec2ops" {
  filename         = data.archive_file.ec2ops_zip.output_path
  function_name    = "${var.env.environment}-${var.ec2ops-lambda.name}"
  role             = aws_iam_role.ec2ops.arn
  memory_size      = local.defaults.memory_size
  timeout          = local.defaults.timeout
  package_type     = "Zip"
  runtime          = "python3.9"
  handler          = "${local.defaults.script_name}.lambda_handler"
  publish          = true
  source_code_hash = data.archive_file.ec2ops_zip.output_base64sha256


  environment {
    variables = {
      env_name         = var.env.environment
      region           = var.env.region
      instance_id      = var.ec2ops-lambda.instance_id
      debug_logs       = var.ec2ops-lambda.debug_logs
      operation        = var.ec2ops-lambda.operation-type
      slack_webhook    = local.defaults.slack_webhook
    }
  }

  #tags = var.tags
}

resource "aws_lambda_function_url" "ec2_ops_url" {
  function_name      = aws_lambda_function.ec2ops.arn
  authorization_type = "NONE"
}

# Lambda ECS Task Cloudwatch Event rule
resource "aws_cloudwatch_event_rule" "ec2ops_rule" {
  name                = "${var.env.environment}-${var.ec2ops-lambda.name}-event-rule"
  description         = "Rule to trigger the Lambda using a cron"
  schedule_expression = var.ec2ops-lambda.cron_expression
}

resource "aws_cloudwatch_event_target" "lambda_ec2_ops" {
  rule      = aws_cloudwatch_event_rule.ec2ops_rule.name
  target_id = "lambda"
  arn       = aws_lambda_function.ec2ops.arn
}

resource "aws_lambda_permission" "cloudwatch_lambda_ec2_start_stop" {
  statement_id  = "AllowExecutionFromCloudWatch"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ec2ops.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.ec2ops_rule.arn
}
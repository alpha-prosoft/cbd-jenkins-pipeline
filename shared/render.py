from jinja2 import Environment, FileSystemLoader, select_autoescape
import os
import json
import argparse
import boto3
from deploy import get_stack_outputs # Assuming deploy.py is in the same directory or python path

def parse_params(param_list):
    """Converts a list of 'key=value' strings into a dictionary."""
    params_dict = {}
    if param_list:
        for item in param_list:
            try:
                key, value = item.split('=', 1)
                params_dict[key] = value
            except ValueError:
                print(f"Warning: Parameter '{item}' is not in 'key=value' format and will be ignored.")
    return params_dict

def get_initial_web_config_from_stacks(aws_region, project_name, environment_name, parent_stacks_csv, stack_params_whitelist_csv=None):
    """
    Fetches outputs from specified CloudFormation parent stacks and returns them as a dictionary.
    Filters outputs based on stack_params_whitelist_csv if provided.
    Stack names are constructed as: {PROJECT_NAME.upper()}-{ENVIRONMENT_NAME.upper()}-{base_stack_name}
    """
    initial_web_config = {}
    if not all([aws_region, project_name, environment_name]):
        print("Warning: AWS region, project name, or environment name not provided. Cannot fetch stack outputs.")
        return initial_web_config

    whitelist = None
    if stack_params_whitelist_csv:
        whitelist = {key.strip() for key in stack_params_whitelist_csv.split(',') if key.strip()}
        print(f"Applying stack parameter whitelist: {whitelist}")

    if parent_stacks_csv:
        parent_stack_base_names = [name.strip() for name in parent_stacks_csv.split(',') if name.strip()]
        if parent_stack_base_names:
            print(f"Processing parent stacks for initial web_config: {parent_stack_base_names}")
            for base_stack_name in parent_stack_base_names:
                try:
                    outputs = get_stack_outputs(aws_region, project_name, environment_name, base_stack_name)
                    if outputs:
                        full_stack_name_for_log = f"{project_name.upper()}-{environment_name.upper()}-{base_stack_name}"
                        if whitelist:
                            filtered_outputs = {k: v for k, v in outputs.items() if k in whitelist}
                            if filtered_outputs:
                                print(f"Adding whitelisted outputs from parent stack '{full_stack_name_for_log}': {filtered_outputs}")
                                initial_web_config.update(filtered_outputs)
                            else:
                                print(f"No whitelisted outputs found in parent stack '{full_stack_name_for_log}'. Original outputs: {outputs}")
                    else:
                        print(f"No outputs found or retrieved for parent stack '{project_name.upper()}-{environment_name.upper()}-{base_stack_name}'.")
                except Exception as e:
                    print(f"Error retrieving outputs for stack '{base_stack_name}': {e}")
        else:
            print("No valid parent stack base names found in --parent-stacks input.")
    return initial_web_config

def main():
    parser = argparse.ArgumentParser(description="Render a Jinja2 template with provided parameters.")
    parser.add_argument("--template-file", 
                        required=True, 
                        help="Path to the Jinja2 template file.")
    parser.add_argument("--output-file", 
                        required=True, 
                        help="Path to the output file where the rendered template will be saved.")
    parser.add_argument("--aws-region",
                        required=True,
                        help="AWS region for fetching stack outputs.")
    parser.add_argument("--project-name",
                        required=True,
                        help="Project name for constructing stack names.")
    parser.add_argument("--environment-name",
                        required=True,
                        help="Environment name for constructing stack names.")
    parser.add_argument("--parent-stacks",
                        required=False,
                        help="Comma-separated list of parent CloudFormation stack base names to fetch outputs from (e.g., 'core-global,vpc-setup').")
    parser.add_argument("--stack-params-whitelist",
                        required=False,
                        help="Comma-separated list of parameter keys to whitelist from parent stack outputs. If provided, only these keys will be included from stack outputs.")
    parser.add_argument("--param", 
                        action='append', 
                        help="Parameters to pass to the template, in 'key=value' format. Can be specified multiple times. These will override values from parent stacks if keys conflict.")

    args = parser.parse_args()

    # Get initial config from parent stack outputs, applying whitelist if provided
    initial_web_config = get_initial_web_config_from_stacks(
        args.aws_region, 
        args.project_name, 
        args.environment_name, 
        args.parent_stacks,
        args.stack_params_whitelist
    )

    # Get config from --param arguments
    cli_params_config = parse_params(args.param)

    # Merge them, with CLI params taking precedence
    web_config = {**initial_web_config, **cli_params_config}
    
    template_dir = os.path.dirname(os.path.abspath(args.template_file))
    template_name = os.path.basename(args.template_file)

    # Set up Jinja2 environment
    env = Environment(
        loader=FileSystemLoader(template_dir),
        autoescape=select_autoescape(['html', 'xml'])
    )

    template = env.get_template(template_name)
    print("Done loading template")

    context = {}
    context['web_config'] = json.dumps(web_config)

    output = template.render(context)
    print("Done rendering tempalte")

    # Write the rendered output to the specified output file
    with open(args.output_file, 'w') as f:
      f.write(output)
    print(f"Successfully rendered template '{args.template_file}' to '{args.output_file}'")


if __name__ == "__main__":
    main()

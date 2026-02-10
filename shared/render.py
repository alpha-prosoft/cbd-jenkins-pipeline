from jinja2 import Environment, FileSystemLoader, BaseLoader, select_autoescape
import os
import json
import argparse
import boto3


def render_template_string(template_string, params_dict, context_key='params'):
    """
    Renders a Jinja2 template string with parameters.
    
    This is a reusable function for rendering templates from strings rather than files.
    It's used by deploy.py to render CloudFormation templates with resolved parameters.
    
    Args:
        template_string: The template content as string
        params_dict: Dictionary of parameters for rendering
        context_key: Key name for params in template context (default: 'params')
        
    Returns:
        str: Rendered template string
        
    Example:
        template = "Hello {{ params.name }}, your ID is {{ params.id }}"
        rendered = render_template_string(template, {"name": "World", "id": "123"})
        # Returns: "Hello World, your ID is 123"
    """
    env = Environment(
        loader=BaseLoader(),
        autoescape=select_autoescape(['html', 'xml', 'yaml', 'json']),
    )
    template = env.from_string(template_string)
    return template.render({context_key: params_dict})


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

def get_initial_web_config_from_stacks(aws_region, environment_name, parent_stacks_csv, resource_name=None, stack_params_whitelist_csv=None):
    """
    Fetches outputs from specified CloudFormation parent stacks and returns them as a dictionary.
    Filters outputs based on stack_params_whitelist_csv if provided.
    Stack names are constructed as: {RESOURCE_NAME.upper()}-{ENVIRONMENT_NAME.upper()}-{base_stack_name} (if resource_name provided)
    or {ENVIRONMENT_NAME.upper()}-{base_stack_name} (if resource_name is None)
    
    Supports per-stack region specification using {stack}@{region} format.
    """
    # Import here to avoid circular import
    from deploy import get_stack_outputs
    
    initial_web_config = {}
    if not all([aws_region, environment_name]):
        print("Warning: AWS region or environment name not provided. Cannot fetch stack outputs.")
        return initial_web_config

    whitelist = None
    if stack_params_whitelist_csv:
        whitelist = {key.strip() for key in stack_params_whitelist_csv.split(',') if key.strip()}
        print(f"Applying stack parameter whitelist: {whitelist}")

    if parent_stacks_csv:
        parent_stack_entries = [entry.strip() for entry in parent_stacks_csv.split(',') if entry.strip()]
        if parent_stack_entries:
            print(f"Processing parent stacks for initial web_config: {parent_stack_entries}")
            for parent_entry in parent_stack_entries:
                # Parse {parent}@{region} format
                if '@' in parent_entry:
                    base_stack_name, stack_region = parent_entry.split('@', 1)
                    base_stack_name = base_stack_name.strip()
                    stack_region = stack_region.strip()
                else:
                    base_stack_name = parent_entry
                    stack_region = aws_region  # Default to deployment region
                
                try:
                    outputs = get_stack_outputs(stack_region, resource_name, environment_name, base_stack_name)
                    if outputs:
                        if resource_name:
                            full_stack_name_for_log = f"{resource_name.upper()}-{environment_name.upper()}-{base_stack_name}"
                        else:
                            full_stack_name_for_log = f"{environment_name.upper()}-{base_stack_name}"
                        if whitelist:
                            filtered_outputs = {k: v for k, v in outputs.items() if k in whitelist}
                            if filtered_outputs:
                                print(f"Adding whitelisted outputs from parent stack '{full_stack_name_for_log}': {filtered_outputs}")
                                initial_web_config.update(filtered_outputs)
                            else:
                                print(f"No whitelisted outputs found in parent stack '{full_stack_name_for_log}'. Original outputs: {outputs}")
                        else:
                            print(f"Adding outputs from parent stack '{full_stack_name_for_log}': {outputs}")
                            initial_web_config.update(outputs)
                    else:
                        if resource_name:
                            stack_name_for_log = f"{resource_name.upper()}-{environment_name.upper()}-{base_stack_name}"
                        else:
                            stack_name_for_log = f"{environment_name.upper()}-{base_stack_name}"
                        print(f"No outputs found or retrieved for parent stack '{stack_name_for_log}'.")
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
    parser.add_argument("--resource-name",
                        required=False,
                        help="Resource name for constructing stack names (optional). If not provided, stack names will be {ENV}-{STACK}.")
    parser.add_argument("--environment-name",
                        required=True,
                        help="Environment name for constructing stack names.")
    parser.add_argument("--parent-stacks",
                        required=False,
                        help="Comma-separated parent stack names with optional region (e.g., 'CORE-global@us-east-1,CORE-vpc,CORE-network@eu-west-1'). Region defaults to --region if not specified.")
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
        args.environment_name, 
        args.parent_stacks,
        args.resource_name,
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

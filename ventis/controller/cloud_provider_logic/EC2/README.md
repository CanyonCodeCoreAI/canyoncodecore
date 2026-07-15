What you need to run agents on EC2. 

For local controller
- AMI with the following installed:
  - Docker
  - Public key in authorizedkeys in ~/.ssh

For global controller
- Instance with all of the following installed:
  - Both things in local controller
  - Python 3.10+
  - Ventis folder
  - pip requirements installed in env
    - pip install -e . --break-system-packages
  - Private key labeled as ventis_ec2 inside ~/.ssh
    - Private key complementing local public key


For convenience, you can use the global controller as the
AMI for local to save some time

AWS Specific Permissions
- IAM Role to attach to global controller instance allowing
  - ec2:RunInstance,TerminateInstance,DescribeInstances,CreateTags
- SSH permissions for entering global controller
- Security group allowing port 50051, 6379, and 22 TCP communication within security group
- Also need the port your workflow API's are open to be allowed


Steps:

1. SSH into EC2 Global Controller Instance

2. Create your app

3. Change the agents/configs/workflow folders to suit your needs

4. Run ventis build + ventis deploy

For cleanup, use ventis clean to clean stubs/containers

If encountering permission errors with the keys, run this to give key more permissions if blocked
chmod 700 ~/.ssh
chmod 600 ~/.ssh/ventis_ec2





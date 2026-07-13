What you need to run agents on EC2. 

- AMI with Docker installed. 
    - Used Ubuntu base image
    - Python/pip too, but only needed for the global controller
-Security group allowing port 50051, 6379, 8080, and 22 communication within the security group
-Passwordless ssh set up.

Steps:

1. SSH into EC2 machine and clone this repo in

2. Install and activate venv

3. Create the app, or navigate to ventis/templates

4. Change the global_controller.yaml configs to suit your setup

5. Run ventis build + ventis deploy

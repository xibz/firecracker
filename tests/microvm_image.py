"""Define a class for interacting with microvm images in s3."""

import os
import re
from shutil import copyfile
from typing import List

import boto3

from microvm import MicrovmSlot


class MicrovmImageS3Fetcher:
    """A borg class for fetching Firecracker microvm images from s3.

    # Microvm Image Bucket Layout

    A microvm image bucket needs to obey a specific layout, described below.

    ## Folder Layout

    ``` tree
    s3://<microvm-image-bucket>/<microvm-image-path>/
        <microvm_image_folder_n>/
            kernel/
                <optional_kernel_name>vmlinux.bin
            fsfiles/
                <rootfs_file_name>rootfs.ext4
                <other_fsfile_n>
                ...
            <other_resource_n>
            ...
        ...
    ```

    ## Tagging

    Microvm image folders are tagged with the capabilities of that image:

    ``` json
    TagSet = [{"key": "capability:<cap_name>", "value": ""}, ...]
    ```

    # Credentials

    When run on an EC2 instance, `boto3` will check for IMDS credentials if no
    other credentials are found. This mechanism is relied upon for running test
    sessions within an account that has access to the microvm-image-bucket. If
    that is not the case, local credentials must be present. See `boto3`
    documentation.
    """

    MICROVM_IMAGES_RELPATH = 'microvm-images/'
    MICROVM_IMAGE_KERNEL_RELPATH = 'kernel/'
    MICROVM_IMAGE_BLOCKDEV_RELPATH = 'fsfiles/'
    MICROVM_IMAGE_KERNEL_FILE_SUFFIX = r'vmlinux.bin'
    MICROVM_IMAGE_ROOTFS_FILE_SUFFIX = r'rootfs.ext4'
    MICROVM_IMAGE_SSH_KEY_SUFFIX = r'.id_rsa'

    ENV_LOCAL_IMAGES_PATH_VAR = 'OPT_LOCAL_IMAGES_PATH'

    CAPABILITY_KEY_PREFIX = 'capability:'

    __shared_state = {}
    """Initializing the shared state here leads to all objects sharing it.

    When you instantiate this class, you get a new object every time, but the
    object's state is the same one as the state of all other objects of this
    class. When an object changes this shared state, all other objects see the
    change. In effect, the s3 bucket will only be mapped once and the s3 client
    can cache across all fixtures. This is called "the borg pattern", because
    in the Sci-Fi series "Star Trek", the borg were a people where all
    individuals that shared the same consciousness.
    """

    microvm_images = None

    def __init__(
        self,
        microvm_images_bucket,
        microvm_images_path=MICROVM_IMAGES_RELPATH
    ):
        """Initialize fetcher shared state, s3 client, paths, and data."""
        self.__dict__ = self.__shared_state

        self.s3 = boto3.client('s3')
        # Will use AWS EC2 IMDS credentials if present.

        self.microvm_images_bucket = microvm_images_bucket
        self.microvm_images_path = microvm_images_path

        if self.microvm_images is None:
            self.map_bucket()

    def get_microvm_image(
        self,
        microvm_image_name,
        microvm_slot: MicrovmSlot
    ):
        """Fetch a microvm image into an existing microvm local slot.

        Assumes the correct microvm image/slot structure, and copies all
        microvm image resources into the microvm slot.
        """
        for resource_key in self.microvm_images[microvm_image_name]:
            if resource_key in [
                self.MICROVM_IMAGE_KERNEL_RELPATH,
                self.MICROVM_IMAGE_BLOCKDEV_RELPATH
            ]:
                # Kernel and blockdev dirs already exist in microvm_slot.
                continue

            slot_dest_path = os.path.join(microvm_slot.path, resource_key)

            if resource_key.endswith('/'):
                # Create a new microvm_slot dir if one is encountered.
                os.mkdir(slot_dest_path)
                continue

            image_rel_path = os.path.join(
                self.microvm_images_path,
                microvm_image_name
            )

            # Relative path of a microvm resource within a microvm directory.
            resource_rel_path = os.path.join(
                image_rel_path,
                resource_key
            )

            if self.ENV_LOCAL_IMAGES_PATH_VAR in os.environ:
                # There's a user-managed local microvm image directory.
                resource_root_path = (
                    os.environ.get(self.ENV_LOCAL_IMAGES_PATH_VAR)
                )
            else:
                # Use a root path in the temporary test session directory.
                resource_root_path = microvm_slot.microvm_root_path

            # Local path of a microvm resource. Used for downloading resources
            # only once.
            resource_local_path = os.path.join(
                resource_root_path,
                resource_rel_path
            )

            if not os.path.exists(resource_local_path):
                # Locally create / download an s3 resource the first time we
                # encounter it.
                os.makedirs(
                    os.path.dirname(resource_local_path),
                    exist_ok=True
                )
                self.s3.download_file(
                    self.microvm_images_bucket,
                    resource_rel_path,
                    resource_local_path)

            copyfile(resource_local_path, slot_dest_path)

            if resource_key.endswith(self.MICROVM_IMAGE_KERNEL_FILE_SUFFIX):
                microvm_slot.kernel_file = slot_dest_path

            if resource_key.endswith(self.MICROVM_IMAGE_ROOTFS_FILE_SUFFIX):
                microvm_slot.rootfs_file = slot_dest_path

            if resource_key.endswith(self.MICROVM_IMAGE_SSH_KEY_SUFFIX):
                # Add the key path to the config dictionary and set
                # permissions.
                microvm_slot.ssh_config['ssh_key_path'] = slot_dest_path
                os.chmod(slot_dest_path, 400)

    def list_microvm_images(self, capability_filter: List[str] = None):
        """Return microvm images with the specified capabilities."""
        capability_filter = capability_filter or ['*']
        microvm_images_with_caps = []
        for cap in capability_filter:
            if cap == '*':
                microvm_images_with_caps.append({*self.microvm_images})
                continue
            microvm_images_with_caps.append(self.microvm_images_by_cap[cap])

        return list(set.intersection(*microvm_images_with_caps))

    def enum_capabilities(self):
        """Return a list of all the capabilities of all microvm images."""
        return [*self.microvm_images_by_cap]

    def map_bucket(self):
        """Map all the keys and tags in the s3 microvm image bucket.

        This allows the other methods to work on local objects.

        Populates `self.microvm_images` with
        {microvm_image_folder_key_n: [microvm_image_key_n, ...], ...}

        Populates `self.microvm_images_by_cap` with a capability dict:
        `{capability_n: {microvm_image_folder_key_n, ...}, ...}
        """
        self.microvm_images = {}
        self.microvm_images_by_cap = {}
        folder_key_groups_regex = re.compile(
            self.microvm_images_path + r'(.+?)/(.*)'
        )

        for obj in self.s3.list_objects_v2(
            Bucket=self.microvm_images_bucket,
            Prefix=self.microvm_images_path
        )['Contents']:
            key_groups = re.match(folder_key_groups_regex, obj['Key'])
            microvm_image_name = key_groups.group(1)
            resource = key_groups.group(2)

            if not resource:
                # This is a microvm image root folder.
                self.microvm_images[microvm_image_name] = []
                for cap in self.get_caps(obj['Key']):
                    if cap not in self.microvm_images_by_cap:
                        self.microvm_images_by_cap[cap] = set()
                    self.microvm_images_by_cap[cap].add(microvm_image_name)
            else:
                # This is key within a microvm image root folder.
                self.microvm_images[microvm_image_name].append(resource)

    def get_caps(self, key):
        """Return the set of capabilities of an s3 object key."""
        tagging = self.s3.get_object_tagging(
            Bucket=self.microvm_images_bucket,
            Key=key
        )
        return {
            tag['Key'][len(self.CAPABILITY_KEY_PREFIX):]
            for tag in tagging['TagSet']
            if tag['Key'].startswith(self.CAPABILITY_KEY_PREFIX)
        }

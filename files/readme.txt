myos volume, built by tools/myfs.py
Every file in this directory is packed into the myfs partition of the hard disk
image, together with a manifest of names, sizes and checksums.  The kernel reads
the manifest back at boot and verifies every file against it, so the host tool
and the kernel have to agree on the format down to the byte.

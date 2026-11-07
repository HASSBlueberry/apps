# 0.1.1

Grant the shell Magisk policy before staging files, treat staging copies as non-fatal so manager install and Zygisk setup are not skipped, and stop the isolated VM guest from building its own Android instance; BlueberryDroid now deploys the single runtime into the guest over SSH.

# 0.1.0

Add a reproducible ReDroid image with bootless Magisk, persistent Android data,
Home Assistant app definition, host launcher, and an isolated KVM guest path.
The direct path stops on SELinux hosts. MyQ App Check issuance remains unverified.

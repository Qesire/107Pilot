"""API package for the 107Pilot service layer."""

from pilot107.api.workarea_launch_extension import install_workarea_launch_extension

# Keep the central HTTP router stable: the delivery slice installs only its
# namespaced dispatch extension and falls through for every existing route.
install_workarea_launch_extension()

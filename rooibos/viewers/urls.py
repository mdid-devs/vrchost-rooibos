from django.conf.urls import patterns, url
from views import viewer_shell, viewer_script, legacy_embedded_video

urlpatterns = patterns(
    '',
    url(
        r'^(?P<viewer>\w+)/(?P<objid>\d+)/(?:[-\w]+/)?$',
        viewer_shell,
        name='viewers-viewer-shell'
    ),
    url(
        r'^(?P<viewer>\w+)/script/(?P<objid>\d+)/$',
        viewer_script,
        name='viewers-viewer-script'
    ),
    # Legacy URL for videos embedded using previous versions of MDID3
    url(r'^embed/(?P<record>\d+)_(?P<media>\d+)\.js$', legacy_embedded_video),
)

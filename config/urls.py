import mimetypes
from pathlib import Path

from django.conf import settings
from django.contrib import admin
from django.http import FileResponse, Http404
from django.urls import path, include, re_path
from django.views.static import serve

FRONTEND_ROOT = Path(settings.BASE_DIR) / 'opd-new frontend'


def serve_frontend_file(request, filepath='index.html'):
    safe_path = Path(filepath)
    if '..' in safe_path.parts:
        raise Http404('Invalid path')
    full_path = FRONTEND_ROOT / safe_path
    if not full_path.exists() or not full_path.is_file():
        raise Http404('Page not found')
    content_type, _ = mimetypes.guess_type(str(full_path))
    response = FileResponse(open(full_path, 'rb'), content_type=content_type or 'text/html')
    if str(filepath).endswith('.html'):
        response['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        response['Pragma'] = 'no-cache'
    return response


# Frontend static assets — must be registered before Django admin at /admin/
_frontend_static = [
    re_path(r'^css/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'css'}),
    re_path(r'^js/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'js'}),
    re_path(r'^images/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'images'}),
    re_path(r'^admin/css/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'css'}),
    re_path(r'^admin/js/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'js'}),
    re_path(r'^admin/images/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'images'}),
    re_path(
        r'^admin/dashboard\.html$',
        serve_frontend_file,
        {'filepath': 'admin/dashboard.html'},
        name='admin-dashboard',
    ),
]

urlpatterns = _frontend_static + [
    path('admin/', admin.site.urls),
    path('api/core/', include('core.urls')),
    re_path(r'^(?P<filepath>.*\.html)$', serve_frontend_file, name='frontend-html'),
    path('', serve_frontend_file, {'filepath': 'index.html'}),
]

if settings.DEBUG:
    urlpatterns += [
        re_path(r'^patient/css/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'css'}),
        re_path(r'^patient/js/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'js'}),
        re_path(r'^reception/css/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'css'}),
        re_path(r'^reception/js/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'js'}),
        re_path(r'^doctor/css/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'css'}),
        re_path(r'^doctor/js/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'js'}),
        re_path(r'^lab/css/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'css'}),
        re_path(r'^lab/js/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'js'}),
        re_path(r'^pharmacy/css/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'css'}),
        re_path(r'^pharmacy/js/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'js'}),
        re_path(r'^staff/css/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'css'}),
        re_path(r'^staff/js/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'js'}),
        re_path(r'^admin/css/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'css'}),
        re_path(r'^admin/js/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'js'}),
        re_path(r'^patient/images/(?P<path>.*)$', serve, {'document_root': FRONTEND_ROOT / 'images'}),
    ]
    urlpatterns += [
        re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
    ]

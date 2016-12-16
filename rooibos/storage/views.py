from __future__ import with_statement
from django.contrib import messages
from datetime import datetime
from django import forms
from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.urlresolvers import resolve, reverse
from django.forms.utils import ErrorList
from django.http import HttpResponse, Http404, HttpResponseRedirect, \
    HttpResponseNotAllowed, HttpResponseForbidden
from django.shortcuts import get_object_or_404, get_list_or_404, \
    render_to_response
from django.template import RequestContext
from django.template.loader import render_to_string
import json as simplejson
from django.views.decorators.cache import cache_control
from django.views.decorators.csrf import csrf_exempt
from django.contrib.contenttypes.models import ContentType
from django.template.defaultfilters import filesizeformat
from models import Media, Storage, TrustedSubnet, ProxyUrl
from rooibos.access import filter_by_access
from ipaddr import IPAddress, IPNetwork
from rooibos.data.models import Collection, Record, FieldValue, \
    CollectionItem, standardfield
from rooibos.storage import get_media_for_record, get_image_for_record, \
    get_thumbnail_for_record, analyze_media, analyze_records, \
    find_record_by_identifier
from rooibos.util import json_view
from rooibos.statistics.models import Activity
from rooibos.workers.models import JobInfo
import logging
import os
import mimetypes


def add_content_length(func):
    def _add_header(request, *args, **kwargs):
        response = func(request, *args, **kwargs)
        if type(response) == HttpResponse:
            if hasattr(response._container, 'size'):
                response['Content-Length'] = response._container.size
            elif isinstance(response._container, file):
                if os.path.exists(response._container.name):
                    response['Content-Length'] = os.path.getsize(
                        response._container.name)
            elif isinstance(getattr(response, 'content', None), basestring):
                response['Content-Length'] = len(response.content)
        return response
    return _add_header


@add_content_length
@cache_control(private=True, max_age=3600)
def retrieve(request, recordid, record, mediaid, media):
    # check if media exists
    mediaobj = get_media_for_record(recordid, request.user).filter(id=mediaid)

    # check download status
    if not mediaobj or not mediaobj[0].is_downloadable_by(request.user):
        return HttpResponseForbidden()
    mediaobj = mediaobj[0]

    try:
        content = mediaobj.load_file()
    except IOError:
        logging.error("mediaobj.load_file() failed for media.id %s" % mediaid)
        raise Http404()

    Activity.objects.create(event='media-download',
                            request=request,
                            content_object=mediaobj)
    if content:
        return HttpResponse(
            content=content,
            content_type=str(mediaobj.mimetype)
        )
    else:
        return HttpResponseRedirect(mediaobj.get_absolute_url())


@add_content_length
@cache_control(private=True, max_age=3600)
def retrieve_image(request, recordid, record, width=None, height=None):

    passwords = request.session.get('passwords', dict())
    force_reprocess = 'reprocess' in request.GET

    path = get_image_for_record(
        recordid,
        request.user,
        int(width or 100000),
        int(height or 100000),
        passwords,
        force_reprocess=force_reprocess
    )
    if not path:
        logging.error(
            "get_image_for_record failed for record.id %s" % recordid)
        raise Http404()

    Activity.objects.create(event='media-download-image',
                            request=request,
                            content_object=Record.objects.get(id=recordid),
                            data=dict(width=width, height=height))
    try:
        response = HttpResponse(
            content=file(path, 'rb').read(),
            content_type='image/jpeg'
        )
        if 'forcedl' in request.GET:
            # if filename was used for record stub, remove extension here to
            # prevent duplication
            if record.endswith('jpg'):
                record = record[:-3]
            response["Content-Disposition"] = \
                "attachment; filename=%s.jpg" % record
        return response
    except IOError:
        logging.error("IOError: %s" % path)
        raise Http404()


def make_storage_select_choice(storage, user):
    limit = storage.get_upload_limit(user)
    if limit != settings.UPLOAD_LIMIT:
        if limit == 0:
            slimit = ' (unlimited)'
        else:
            slimit = ' (max %s)' % filesizeformat(limit * 1024)
    else:
        slimit = ''
    return ('%s,%s' % (storage.id, limit),
            '%s%s' % (storage.title, slimit))


def media_upload_form(request):
    available_storage = filter_by_access(
        request.user, Storage, write=True).order_by('title')
    if not available_storage:
        return None

    choices = [
        make_storage_select_choice(s, request.user)
        for s in available_storage
    ]

    class UploadFileForm(forms.Form):
        storage = forms.ChoiceField(choices=choices)
        file = forms.FileField()

    return UploadFileForm


@csrf_exempt
@login_required
def media_upload(request, recordid, record):
    record = Record.get_or_404(recordid, request.user)
    if not record.editable_by(request.user):
        raise Http404()

    if request.method == 'POST':

        upload_file_form = media_upload_form(request)
        if not upload_file_form:
            raise Http404()

        form = upload_file_form(request.POST, request.FILES)
        if form.is_valid():

            storage = Storage.objects.get(
                id=form.cleaned_data['storage'].split(',')[0])
            file = request.FILES['file']
            mimetype = mimetypes.guess_type(file.name)[0] or file.content_type

            limit = storage.get_upload_limit(request.user)
            if limit > 0 and file.size > limit * 1024:
                messages.add_message(
                    request,
                    messages.INFO,
                    message="The uploaded file is too large."
                )
                return HttpResponseRedirect(
                    request.GET.get('next', reverse('main')))

            media = Media.objects.create(record=record,
                                         name=os.path.splitext(file.name)[0],
                                         storage=storage,
                                         mimetype=mimetype)
            media.save_file(file.name, file)

            if request.POST.get('swfupload') == 'true':
                html = render_to_string(
                    'storage_import_file_response.html',
                    {
                        'result': 'saved',
                        'record': record,
                        'sidebar': 'sidebar' in request.GET,
                    },
                    context_instance=RequestContext(request)
                )
                return HttpResponse(
                    content=simplejson.dumps(dict(status='ok', html=html)),
                    content_type='application/json'
                )

            return HttpResponseRedirect(
                request.GET.get('next', reverse('main')))
        else:
            # Invalid form submission
            raise Http404()
    else:
        return HttpResponseNotAllowed(['POST'])


@login_required
def media_delete(request, mediaid, medianame):
    media = get_object_or_404(Media, id=mediaid)
    if not media.editable_by(request.user):
        raise Http404()
    if request.method == 'POST':
        media.delete()
        return HttpResponseRedirect(request.GET.get('next', '.'))
    else:
        return HttpResponseNotAllowed(['POST'])


@add_content_length
@cache_control(private=True, max_age=3600)
def record_thumbnail(request, id, name):
    filename = get_thumbnail_for_record(
        id, request.user, crop_to_square='square' in request.GET)
    if filename:
        Activity.objects.create(
            event='media-thumbnail',
            request=request,
            content_type=ContentType.objects.get_for_model(Record),
            object_id=id,
            data=dict(
                square=int('square' in request.GET)
            )
        )
        try:
            return HttpResponse(
                content=open(filename, 'rb').read(),
                content_type='image/jpeg'
            )
        except IOError:
            logging.error("IOError: %s" % filename)
    return HttpResponseRedirect(
        reverse('static', args=('images/thumbnail_unavailable.png',)))


@json_view
def create_proxy_url_view(request):
    if request.method == 'POST':
        if 'url' in request.POST and 'context' in request.POST:
            proxy_url = ProxyUrl.create_proxy_url(
                request.POST['url'],
                request.POST['context'],
                request.META['REMOTE_ADDR'],
                request.user
            )
            if proxy_url:
                return dict(id=proxy_url.uuid)
        return dict(
            result='error',
            message='Invalid request. Proxy URL could not be created.'
        )
    else:
        return HttpResponseNotAllowed(['POST'])


def create_proxy_url_if_needed(url, request):
    if hasattr(request, 'proxy_url'):
        return request.proxy_url.get_additional_url(url).get_absolute_url()
    else:
        return url


def call_proxy_url(request, uuid):
    context = request.GET.get('context')

    ip = IPAddress(request.META['REMOTE_ADDR'])
    for subnet in TrustedSubnet.objects.all():
        if ip in IPNetwork(subnet.subnet):
            break
    else:
        return HttpResponseForbidden()

    proxy_url = get_object_or_404(
        ProxyUrl.objects.filter(uuid=uuid, context=context, subnet=subnet))
    proxy_url.last_access = datetime.now()
    proxy_url.save()

    view, args, kwargs = resolve(proxy_url.url)

    user = proxy_url.user
    user.backend = proxy_url.user_backend or \
        settings.AUTHENTICATION_BACKENDS[0]
    login(request, user)

    request.proxy_url = proxy_url
    kwargs['request'] = request

    return view(*args, **kwargs)


@login_required
def manage_storages(request):

    storages = filter_by_access(
        request.user, Storage, manage=True).order_by('title')

    for s in storages:
        s.analysis_available = hasattr(s, 'get_files')

    return render_to_response(
        'storage_manage.html',
        {
            'storages': storages,
        },
        context_instance=RequestContext(request)
    )


@login_required
def manage_storage(request, storageid=None, storagename=None):

    if storageid and storagename:
        storage = get_object_or_404(
            filter_by_access(request.user, Storage, manage=True), id=storageid)
    else:
        storage = Storage(system='local')

    if not storage.id:
        system_choices = [(s, s) for s in settings.STORAGE_SYSTEMS.keys()]
    else:
        system_choices = [(storage.system, storage.system)]

    class StorageForm(forms.ModelForm):
        system = forms.CharField(widget=forms.Select(choices=system_choices))

        def clean_system(self):
            if not self.instance.id:
                return self.cleaned_data['system']
            else:
                return self.instance.system

        class Meta:
            model = Storage
            fields = ('title', 'system', 'base', 'urlbase', 'deliverybase')

    if request.method == "POST":
        if request.POST.get('delete-storage'):
            if not request.user.is_superuser:
                raise HttpResponseForbidden()
            messages.add_message(
                request,
                messages.INFO,
                message="Storage '%s' has been deleted." % storage.title
            )
            storage.delete()
            return HttpResponseRedirect(reverse('storage-manage'))
        else:
            form = StorageForm(request.POST, instance=storage)
            if form.is_valid():
                form.save()
                return HttpResponseRedirect(
                    reverse(
                        'storage-manage-storage',
                        kwargs=dict(
                            storageid=form.instance.id,
                            storagename=form.instance.name)
                    )
                )
    else:
        form = StorageForm(instance=storage)

    return render_to_response(
        'storage_edit.html',
        {
            'storage': storage,
            'form': form,
        },
        context_instance=RequestContext(request)
    )


@csrf_exempt
@login_required
def import_files(request):

    available_storage = get_list_or_404(
        filter_by_access(request.user, Storage, write=True).order_by('title'))
    available_collections = get_list_or_404(
        filter_by_access(request.user, Collection))
    writable_collection_ids = list(
        filter_by_access(request.user, Collection, write=True).values_list(
            'id', flat=True))

    storage_choices = [
        make_storage_select_choice(s, request.user) for s in available_storage
    ]

    class UploadFileForm(forms.Form):
        collection = forms.ChoiceField(
            choices=(
                (
                    c.id,
                    '%s%s' % (
                        '*' if c.id in writable_collection_ids else '', c.title
                    )
                )
                for c in sorted(available_collections, key=lambda c: c.title)
            )
        )
        storage = forms.ChoiceField(choices=storage_choices)
        file = forms.FileField()
        create_records = forms.BooleanField(required=False)
        replace_files = forms.BooleanField(
            required=False, label='Replace files of same type')
        multiple_files = forms.BooleanField(
            required=False, label='Allow multiple files of same type')
        personal_records = forms.BooleanField(required=False)

        def clean(self):
            cleaned_data = self.cleaned_data
            if any(self.errors):
                return cleaned_data
            personal = cleaned_data['personal_records']
            if not personal:
                if int(cleaned_data['collection']) not in \
                        writable_collection_ids:
                    self._errors['collection'] = \
                        ErrorList([
                            "Can only add personal records "
                            "to selected collection"
                        ])
                    del cleaned_data['collection']
            return cleaned_data

    if request.method == 'POST':

        form = UploadFileForm(request.POST, request.FILES)
        if form.is_valid():

            create_records = form.cleaned_data['create_records']
            replace_files = form.cleaned_data['replace_files']
            multiple_files = form.cleaned_data['multiple_files']
            personal_records = form.cleaned_data['personal_records']

            collection = get_object_or_404(
                filter_by_access(
                    request.user,
                    Collection.objects.filter(
                        id=form.cleaned_data['collection']
                    ),
                    write=True if not personal_records else None
                )
            )
            storage = get_object_or_404(
                filter_by_access(
                    request.user,
                    Storage.objects.filter(
                        id=form.cleaned_data['storage'].split(',')[0]
                    ),
                    write=True
                )
            )
            file = request.FILES['file']
            record = None

            limit = storage.get_upload_limit(request.user)
            if limit > 0 and file.size > limit * 1024:
                result = "The uploaded file is too large (%d>%d)." % (
                    file.size, limit * 1024)
            else:

                mimetype = mimetypes.guess_type(file.name)[0] or \
                    file.content_type

                owner = request.user if personal_records else None
                id = os.path.splitext(file.name)[0]

                # find record by identifier
                titlefield = standardfield('title')
                idfield = standardfield('identifier')

                # Match identifiers that are either full file name
                # (with extension) or just base name match
                records = find_record_by_identifier(
                    (id, file.name,),
                    collection,
                    owner=owner,
                    ignore_suffix=multiple_files
                )
                result = "File skipped."

                if len(records) == 1:
                    # Matching record found
                    record = records[0]
                    media = record.media_set.filter(
                        storage=storage, mimetype=mimetype)
                    media_same_id = media.filter(name=id)
                    if len(media) == 0 or \
                            (len(media_same_id) == 0 and multiple_files):
                        # No media yet
                        media = Media.objects.create(record=record,
                                                     name=id,
                                                     storage=storage,
                                                     mimetype=mimetype)
                        media.save_file(file.name, file)
                        result = "File added (Identifier '%s')." % id
                    elif len(media_same_id) > 0 and multiple_files:
                        # Replace existing media with same name and mimetype
                        media = media_same_id[0]
                        media.delete_file()
                        media.save_file(file.name, file)
                        result = "File replaced (Identifier '%s')." % id
                    elif replace_files:
                        # Replace existing media with same mimetype
                        media = media[0]
                        media.delete_file()
                        media.save_file(file.name, file)
                        result = "File replaced (Identifier '%s')." % id
                    else:
                        result = "File skipped, media files already attached."
                elif len(records) == 0:
                    # No matching record found
                    if create_records:
                        # Create a record
                        record = Record.objects.create(name=id, owner=owner)
                        CollectionItem.objects.create(
                            collection=collection, record=record)
                        FieldValue.objects.create(
                            record=record, field=idfield, value=id, order=0)
                        FieldValue.objects.create(
                            record=record, field=titlefield, value=id, order=1)
                        media = Media.objects.create(record=record,
                                                     name=id,
                                                     storage=storage,
                                                     mimetype=mimetype)
                        media.save_file(file.name, file)
                        result = \
                            "File added to new record (Identifier '%s')." % id
                    else:
                        result = \
                            "File skipped, no matching record found " \
                            "(Identifier '%s')." % id
                else:
                    result = \
                        "File skipped, multiple matching records found " \
                        "(Identifier '%s')." % id
                    # Multiple matching records found
                    pass

            if request.POST.get('swfupload') == 'true':
                html = render_to_string(
                    'storage_import_file_response.html',
                    {
                        'result': result,
                        'record': record,
                    },
                    context_instance=RequestContext(request)
                )
                return HttpResponse(
                    content=simplejson.dumps(dict(status='ok', html=html)),
                    content_type='application/json'
                )

            messages.add_message(
                request,
                messages.INFO,
                message=result
            )
            next = request.GET.get('next', request.get_full_path())
            return HttpResponseRedirect(next)

        else:
            # invalid form submission
            if request.POST.get('swfupload') == 'true':
                html = render_to_string(
                    'storage_import_file_response.html',
                    {
                        'result': form.errors
                    },
                    context_instance=RequestContext(request)
                )
                return HttpResponse(
                    content=simplejson.dumps(dict(status='ok', html=html)),
                    content_type='application/json'
                )

    else:
        form = UploadFileForm()

    return render_to_response(
        'storage_import_files.html',
        {
            'upload_form': form,
        },
        context_instance=RequestContext(request)
    )


@login_required
def match_up_files(request):
    available_storage = get_list_or_404(
        filter_by_access(
            request.user,
            Storage,
            manage=True
        ).order_by('title').values_list('id', 'title')
    )
    available_collections = get_list_or_404(
        filter_by_access(request.user, Collection, manage=True))

    class MatchUpForm(forms.Form):
        collection = forms.ChoiceField(
            choices=(
                (c.id, c.title)
                for c in sorted(available_collections, key=lambda c: c.title)
            )
        )
        storage = forms.ChoiceField(choices=available_storage)
        allow_multiple_use = forms.BooleanField(required=False)

    if request.method == 'POST':

        form = MatchUpForm(request.POST)
        if form.is_valid():

            collection = get_object_or_404(
                filter_by_access(
                    request.user,
                    Collection.objects.filter(
                        id=form.cleaned_data['collection']
                    ),
                    manage=True
                )
            )
            storage = get_object_or_404(
                filter_by_access(
                    request.user,
                    Storage.objects.filter(
                        id=form.cleaned_data['storage']
                    ),
                    manage=True
                )
            )

            job = JobInfo.objects.create(
                owner=request.user,
                func='storage_match_up_media',
                arg=simplejson.dumps(dict(
                    collection=collection.id,
                    storage=storage.id,
                    allow_multiple_use=form.cleaned_data['allow_multiple_use']
                ))
            )
            job.run()

            messages.add_message(
                request,
                messages.INFO,
                message='Match up media job has been submitted.'
            )
            return HttpResponseRedirect(
                "%s?highlight=%s" % (reverse('workers-jobs'), job.id))
    else:
        form = MatchUpForm(request.GET)

    return render_to_response('storage_match_up_files.html',
                              {'form': form,
                               },
                              context_instance=RequestContext(request))


@login_required
def analyze(request, id, name, allow_multiple_use=True):
    storage = get_object_or_404(filter_by_access(
        request.user, Storage.objects.filter(id=id), manage=True))
    broken, extra = analyze_media(storage, allow_multiple_use)
    broken = [m.url for m in broken]
    return render_to_response(
        'storage_analyze.html',
        {
            'storage': storage,
            'broken': sorted(broken),
            'extra': sorted(extra),
        },
        context_instance=RequestContext(request)
    )


@login_required
def find_records_without_media(request):
    available_storage = get_list_or_404(
        filter_by_access(
            request.user,
            Storage,
            manage=True
        ).order_by('title').values_list('id', 'title')
    )
    available_collections = get_list_or_404(
        filter_by_access(request.user, Collection, manage=True))

    class SelectionForm(forms.Form):
        collection = forms.ChoiceField(
            choices=(
                (c.id, c.title)
                for c in sorted(available_collections, key=lambda c: c.title)
            )
        )
        storage = forms.ChoiceField(choices=available_storage)

    identifiers = records = []
    analyzed = False

    if request.method == 'POST':

        form = SelectionForm(request.POST)
        if form.is_valid():

            collection = get_object_or_404(
                filter_by_access(
                    request.user,
                    Collection.objects.filter(
                        id=form.cleaned_data['collection']
                    ),
                    manage=True
                )
            )
            storage = get_object_or_404(
                filter_by_access(
                    request.user,
                    Storage.objects.filter(
                        id=form.cleaned_data['storage']
                    ),
                    manage=True
                )
            )

            records = analyze_records(collection, storage)
            analyzed = True

            identifiers = FieldValue.objects.filter(
                field__in=standardfield('identifier', equiv=True),
                record__in=records
            ).order_by('value').values_list('value', flat=True)

    else:
        form = SelectionForm(request.GET)

    return render_to_response(
        'storage_find_records_without_media.html',
        {
            'form': form,
            'identifiers': identifiers,
            'records': records,
            'analyzed': analyzed,
        },
        context_instance=RequestContext(request)
    )

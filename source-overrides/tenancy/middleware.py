from .models import Branch
from .scope import current_branch

class BranchMiddleware:
    def __init__(self,get_response):self.get_response=get_response
    def __call__(self,request):
        if request.path_info in ('/health/', '/manifest.webmanifest', '/service-worker.js'):
            return self.get_response(request)
        selected=request.session.get('branch_id')
        branch=Branch.objects.filter(pk=selected).first() if isinstance(selected,int) else None
        if branch is None:
            branch = Branch.objects.order_by('pk').first()
            if branch is None:
                branch, _ = Branch.objects.get_or_create(name='Cabang Utama')
        posted=request.POST.get('_branch_id') if request.method=='POST' else None
        if posted is not None and posted!=str(branch.pk) and request.path!='/branches/switch/':
            from django.http import HttpResponse
            return HttpResponse('Cabang aktif berubah di tab lain. Buka ulang halaman dari dashboard sebelum menyimpan. <a href="/">Kembali ke dashboard</a>',status=409)
        request.branch=branch
        token=current_branch.set(branch.pk)
        try:return self.get_response(request)
        finally:current_branch.reset(token)

def branch_context(request):
    return {'branches':Branch.objects.all(),'selected_branch':getattr(request,'branch',None)}

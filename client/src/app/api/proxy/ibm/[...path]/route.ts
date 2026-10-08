import { NextRequest } from 'next/server';
import { forwardRequest } from '@/lib/backend-proxy';

async function handler(
  request: NextRequest,
  { params }: { params: Promise<{ path: string[] }> },
) {
  const { path } = await params;
  // IBM routes are registered at the backend root as /ibm/...
  const backendPath = '/ibm/' + path.join('/');
  return forwardRequest(request, request.method, backendPath, 'ibm');
}

export { handler as GET, handler as POST, handler as DELETE };

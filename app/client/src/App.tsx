import { useEffect, useState } from 'react';
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Skeleton,
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from '@databricks/appkit-ui/react';
import { ShieldCheck } from 'lucide-react';
import { api, describeError, type Me } from './lib/api';
import { ReviewQueue } from './pages/ReviewQueue';
import { ScopeRegistry } from './pages/ScopeRegistry';
import { SuggestionMapping } from './pages/SuggestionMapping';

export default function App() {
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<string[] | null>(null);
  const batch = new URLSearchParams(window.location.search).get('batch') ?? undefined;

  useEffect(() => {
    api.me().then(setMe, (err: unknown) => setError(describeError(err)));
  }, []);

  const isOwner = !!me?.scopes.some((s) => s.roles.includes('GOVERNANCE_OWNER'));
  const isReviewer = !!me?.scopes.some((s) => s.roles.length > 0);
  const showPrivacy = !!me && (me.isGovernance || isOwner);

  return (
    <div className="min-h-screen bg-background flex flex-col">
      <header className="border-b px-4 md:px-6 py-3 flex items-center gap-3">
        <ShieldCheck className="h-5 w-5 text-primary" />
        <h1 className="text-lg font-semibold text-foreground">PHI Classification Review</h1>
        {me && (
          <div className="ml-auto flex items-center gap-2 text-sm text-muted-foreground">
            <span>{me.email}</span>
            {me.isGovernance && <Badge>Data Governance</Badge>}
            {isOwner && <Badge variant="secondary">Governance owner</Badge>}
            {isReviewer && !isOwner && <Badge variant="secondary">Steward</Badge>}
            <Badge variant="outline">{me.env}</Badge>
          </div>
        )}
      </header>
      <main className="flex-1 p-4 md:p-6">
        {error && (
          <Alert variant="destructive">
            <AlertTitle>Could not identify you</AlertTitle>
            <AlertDescription>{error.join('; ')}</AlertDescription>
          </Alert>
        )}
        {!me && !error && <Skeleton className="h-96 w-full" />}
        {me && (
          <Tabs defaultValue="review">
            <TabsList>
              <TabsTrigger value="review">My reviews</TabsTrigger>
              {showPrivacy && <TabsTrigger value="privacy">Privacy review</TabsTrigger>}
              <TabsTrigger value="scopes">Scope registry</TabsTrigger>
              <TabsTrigger value="mapping">Suggestion mapping</TabsTrigger>
            </TabsList>
            <TabsContent value="review">
              {isReviewer ? (
                <ReviewQueue kind="review" batchFilter={batch} />
              ) : (
                <p className="text-sm text-muted-foreground mt-4">
                  You are not a steward or governance owner for any active scope in {me.env}.
                </p>
              )}
            </TabsContent>
            {showPrivacy && (
              <TabsContent value="privacy">
                <ReviewQueue kind="privacy" batchFilter={batch} />
              </TabsContent>
            )}
            <TabsContent value="scopes">
              <ScopeRegistry canEdit={me.isGovernance} />
            </TabsContent>
            <TabsContent value="mapping">
              <SuggestionMapping canEdit={me.isGovernance} />
            </TabsContent>
          </Tabs>
        )}
      </main>
    </div>
  );
}

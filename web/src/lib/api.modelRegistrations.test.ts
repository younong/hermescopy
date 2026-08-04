// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  api,
  type ModelRegistrationRequest,
} from "./api";

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

const request: ModelRegistrationRequest = {
  name: "Image model",
  kind: "image",
  source: "catalog",
  provider: "image-provider",
  model: "image-v1",
  use_gateway: true,
};

beforeEach(() => {
  window.__HERMES_AUTH_REQUIRED__ = true;
});

afterEach(() => {
  delete window.__HERMES_AUTH_REQUIRED__;
  vi.restoreAllMocks();
});

describe("model registration API", () => {
  it("uses the current owner's registration contract for list and CRUD", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.resolve(
        response({
          registrations: [],
          active: {},
        }),
      ),
    );

    await api.getModelRegistrations();
    await api.getModelRegistrationCatalog("image");
    await api.createModelRegistration(request);
    await api.updateModelRegistration("registration/a", request);
    await api.deleteModelRegistration("registration/a");

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/model/registrations",
      "/api/model/registrations/catalog?kind=image",
      "/api/model/registrations",
      "/api/model/registrations",
      "/api/model/registrations",
    ]);
    expect(fetchMock.mock.calls[2]?.[1]).toMatchObject({
      method: "POST",
      body: JSON.stringify(request),
    });
    expect(fetchMock.mock.calls[3]?.[1]).toMatchObject({
      method: "PUT",
      body: JSON.stringify({ ...request, id: "registration/a" }),
    });
    expect(fetchMock.mock.calls[4]?.[1]).toMatchObject({
      method: "DELETE",
      body: JSON.stringify({ id: "registration/a" }),
    });
  });

  it("sends activation to the current owner's dedicated endpoint", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(response({ ok: true }));

    await api.activateModelRegistration("image-a");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/model/registrations/active",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ id: "image-a" }),
      }),
    );
  });
});

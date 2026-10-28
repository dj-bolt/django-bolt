//! Bypass Actix's path allocation and regex for paths without repeated slashes.

use std::rc::Rc;
use std::task::{Context, Poll};

use actix_web::Error;
use actix_web::dev::{Service, ServiceRequest, ServiceResponse, Transform};
use actix_web::middleware::{NormalizePath, TrailingSlash};
use futures_util::future::LocalBoxFuture;

pub struct MergeSlashes;

pub struct MergeSlashesService<S>
where
    NormalizePath: Transform<Rc<S>, ServiceRequest>,
{
    service: Rc<S>,
    normalized: <NormalizePath as Transform<Rc<S>, ServiceRequest>>::Transform,
}

impl<S, B> Transform<S, ServiceRequest> for MergeSlashes
where
    S: Service<ServiceRequest, Response = ServiceResponse<B>, Error = Error> + 'static,
    S::Future: 'static,
    B: 'static,
{
    type Response = ServiceResponse<B>;
    type Error = Error;
    type InitError = ();
    type Transform = MergeSlashesService<S>;
    type Future = LocalBoxFuture<'static, Result<Self::Transform, Self::InitError>>;

    fn new_transform(&self, service: S) -> Self::Future {
        Box::pin(async move {
            let service = Rc::new(service);
            let normalized = NormalizePath::new(TrailingSlash::MergeOnly)
                .new_transform(service.clone())
                .await?;
            Ok(MergeSlashesService {
                service,
                normalized,
            })
        })
    }
}

impl<S, B> Service<ServiceRequest> for MergeSlashesService<S>
where
    S: Service<ServiceRequest, Response = ServiceResponse<B>, Error = Error>,
    S::Future: 'static,
{
    type Response = ServiceResponse<B>;
    type Error = Error;
    type Future = S::Future;

    fn poll_ready(&self, cx: &mut Context<'_>) -> Poll<Result<(), Self::Error>> {
        self.service.poll_ready(cx)
    }

    fn call(&self, request: ServiceRequest) -> Self::Future {
        if request
            .path()
            .as_bytes()
            .windows(2)
            .any(|pair| pair == b"//")
        {
            self.normalized.call(request)
        } else {
            self.service.call(request)
        }
    }
}

/**
 * ImageBlock：图片块（image_url/input_image/image 三别名同族）。
 */

import { memo } from "react";

interface Props {
  url: string;
  alt?: string;
}

export const ImageBlock = memo(function ImageBlock({ url, alt }: Props) {
  return (
    <figure className="image-block">
      <img src={url} alt={alt ?? ""} loading="lazy" />
      {alt && <figcaption>{alt}</figcaption>}
    </figure>
  );
});
